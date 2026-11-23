"""Durable conversation journal, bounded full-session audio and portable exports.

Microphone and playback tracks are deliberately separate. Playback WAV contains
only the chunks actually submitted to the audio output; its timeline makes the
silent gaps explicit. It does not assert that a particular sound was heard.
"""
import copy
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex[:8] + '.tmp')
    with temporary.open('w', encoding='utf-8') as file:
        os.chmod(temporary, 0o600)
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


class EventJournal:
    """Append-only updates survive interruptions; latest events can be replayed."""
    def __init__(self, directory, *, clock=time.monotonic):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'events.jsonl'
        self.clock, self.origin = clock, clock()
        self.lock = threading.RLock()
        self.events = {}
        self.order = []
        if self.path.exists():
            raw = self.path.read_bytes()
            if raw and not raw.endswith(b'\n'):
                with self.path.open('ab') as file:
                    file.write(b'\n')
            for line in self.path.read_text(encoding='utf-8').splitlines():
                try:
                    entry = json.loads(line)
                    if entry['operation'] == 'create':
                        self.events[entry['event']['id']] = entry['event']
                        self.order.append(entry['event']['id'])
                    elif entry['operation'] == 'update' and entry['id'] in self.events:
                        self.events[entry['id']].update(entry['fields'])
                except (ValueError, TypeError, KeyError):
                    # A power loss can leave the final JSONL record incomplete.
                    continue

    def _append(self, record):
        with self.path.open('a', encoding='utf-8') as file:
            os.chmod(self.path, 0o600)
            file.write(json.dumps(record, ensure_ascii=False) + '\n')
            file.flush()
            os.fsync(file.fileno())

    def record(self, role, text, question_id=None, **metadata):
        if role not in {'user', 'assistant', 'system'} or not isinstance(text, str):
            raise ValueError('Conversation event requires a valid role and text')
        with self.lock:
            event_id = 'event-' + uuid.uuid4().hex
            event = dict(metadata, id=event_id, role=role, text=text,
                         question_id=question_id, at=datetime.now(timezone.utc).isoformat(),
                         session_ms=round((self.clock() - self.origin) * 1000, 3))
            if role == 'assistant':
                event.setdefault('delivery', 'planned')
            self._append({'operation': 'create', 'event': event})
            self.events[event_id] = event
            self.order.append(event_id)
            return event_id

    def update(self, event_id, **fields):
        if any(key in fields for key in ('id', 'role', 'text', 'question_id')):
            raise ValueError('Identity and original text of an event cannot change')
        with self.lock:
            if event_id not in self.events:
                raise KeyError(event_id)
            self._append({'operation': 'update', 'id': event_id, 'fields': fields})
            self.events[event_id].update(fields)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy([self.events[event_id] for event_id in self.order])

    def save_transcript(self):
        events = self.snapshot()
        atomic_json(self.directory / 'transcript.json', {
            'schema_version': '1.0', 'events': events,
            'timing': 'session_ms uses monotonic time relative to journal creation',
            'playback_semantics': 'submitted audio chunks are not proof of acoustic audibility',
        })
        return events


class FullSessionRecorder:
    """Nonblocking producer -> bounded queue -> one WAV/timeline writer.

    enqueue('microphone', samples, rate) is suitable for capture callbacks. Call
    enqueue('assistant', actual_chunk, rate, event_id=...) AFTER output.write.
    Queue overrun is explicit in the manifest and makes complete=False.
    """
    def __init__(self, directory, *, queue_blocks=512, clock=time.monotonic, origin=None):
        if queue_blocks < 1:
            raise ValueError('queue_blocks must be positive')
        self.directory = Path(directory)
        self.audio_directory = self.directory / 'full_audio'
        self.audio_directory.mkdir(parents=True, exist_ok=True)
        self.clock, self.origin = clock, clock() if origin is None else origin
        self.queue = queue.Queue(maxsize=queue_blocks)
        self.stopping = threading.Event()
        self.closed = False
        self.error = None
        self.dropped_blocks = 0
        self.dropped_frames = 0
        self.tracks = {}
        self._files = {}
        self.thread = threading.Thread(target=self._write, name='session-audio-writer', daemon=True)
        self.thread.start()

    def enqueue(self, speaker, samples, sample_rate, *, event_id=None, monotonic_time=None):
        if self.stopping.is_set():
            return False
        if speaker not in {'microphone', 'assistant'} or type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError('Invalid audio speaker or sample rate')
        import numpy as np
        audio = np.asarray(samples, dtype=np.float32)
        if audio.ndim not in {1, 2} or (audio.ndim == 2 and not 1 <= audio.shape[1] <= 8):
            raise ValueError('Audio must be mono or a frames/channels matrix')
        if not len(audio):
            return True
        # Copy because PortAudio owns/reuses the callback buffer.
        audio = audio.copy()
        timestamp = self.clock() if monotonic_time is None else monotonic_time
        item = (speaker, audio, sample_rate, timestamp, event_id)
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            self.dropped_blocks += 1
            self.dropped_frames += len(audio)
            return False

    def _write(self):
        import soundfile as sf
        timeline_path = self.audio_directory / 'timeline.jsonl'
        try:
            with timeline_path.open('w', encoding='utf-8') as timeline:
                timeline_path.chmod(0o600)
                while not self.stopping.is_set() or not self.queue.empty():
                    try:
                        speaker, audio, rate, timestamp, event_id = self.queue.get(timeout=.05)
                    except queue.Empty:
                        continue
                    channels = 1 if audio.ndim == 1 else audio.shape[1]
                    key = f'{speaker}-{rate}Hz-{channels}ch'
                    if key not in self._files:
                        relative = f'full_audio/{key}.wav'
                        file = sf.SoundFile(str(self.directory / relative), mode='w', samplerate=rate,
                                            channels=channels, format='WAV', subtype='PCM_16')
                        (self.directory / relative).chmod(0o600)
                        self._files[key] = file
                        self.tracks[key] = dict(speaker=speaker, path=relative, sample_rate=rate,
                                                channels=channels, frames=0, chunks=0)
                    track = self.tracks[key]
                    offset = track['frames']
                    self._files[key].write(audio)
                    track['frames'] += len(audio)
                    track['chunks'] += 1
                    timeline.write(json.dumps({
                        'track': key, 'session_ms': round((timestamp - self.origin) * 1000, 3),
                        'frame_offset': offset, 'frames': len(audio),
                        'duration_ms': len(audio) * 1000 / rate, 'event_id': event_id,
                    }, ensure_ascii=False) + '\n')
                timeline.flush()
                os.fsync(timeline.fileno())
        except Exception as error:
            self.error = str(error)
            self.stopping.set()
        finally:
            for file in self._files.values():
                try:
                    file.flush()
                    file.close()
                except Exception as error:
                    self.error = self.error or str(error)

    def snapshot(self):
        # Writer is the only track mutator; snapshot is normally called after close.
        tracks = [dict(value) for value in list(self.tracks.copy().values())]
        captured_input = any(track['speaker'] == 'microphone' and track['frames'] > 0 for track in tracks)
        return {'schema_version': '1.0', 'complete': self.closed and captured_input and not self.error and not self.dropped_blocks,
                'microphone_captured': captured_input,
                'closed': self.closed, 'error': self.error, 'dropped_blocks': self.dropped_blocks,
                'dropped_frames': self.dropped_frames, 'tracks': tracks,
                'timeline_path': 'full_audio/timeline.jsonl',
                'layout': 'separate compact tracks; use timeline to reconstruct gaps and overlap',
                'assistant_audio': 'chunks submitted to output; not proof of speaker audibility',
                'microphone_audio': 'continuous input supplied by capture callback, including silence'}

    def close(self, timeout=15):
        if self.closed:
            return self.snapshot()
        self.stopping.set()
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            raise RuntimeError('Full-session audio writer did not finish; export is not yet complete')
        self.closed = True
        manifest = self.snapshot()
        atomic_json(self.audio_directory / 'manifest.json', manifest)
        return manifest

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def export_session(directory, *, data=None, journal=None, archive=True):
    """Create downstream JSON + optional ZIP without recursively including ZIPs."""
    directory = Path(directory).resolve()
    answers_path = directory / 'answers.json'
    answers = copy.deepcopy(data) if data is not None else json.loads(answers_path.read_text(encoding='utf-8'))
    if journal:
        events = journal.save_transcript()
    elif (directory / 'events.jsonl').is_file():
        events = EventJournal(directory).save_transcript()
    else:
        events = []
        atomic_json(directory / 'transcript.json', {'schema_version': '1.0', 'events': []})
    manifest_path = directory / 'full_audio/manifest.json'
    audio_manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else None
    video_path = directory / 'video/manifest.json'
    video_manifest = json.loads(video_path.read_text()) if video_path.is_file() else None
    media = []
    candidates = []
    for name in ('audio', 'full_audio', 'video'):
        folder = directory / name
        if folder.is_dir():
            candidates.extend(sorted(folder.rglob('*')))
    for file in candidates:
        resolved = file.resolve()
        if file.is_symlink() or not file.is_file() or not resolved.is_relative_to(directory):
            continue
        item = {'path': file.relative_to(directory).as_posix(), 'bytes': file.stat().st_size}
        if file.suffix == '.wav':
            import soundfile as sf
            try:
                info = sf.info(file)
                item.update(sample_rate=info.samplerate, channels=info.channels,
                            frames=info.frames, duration_s=info.duration)
            except (RuntimeError, OSError) as error:
                item['metadata_error'] = str(error)
        media.append(item)
    package = {'schema_version': 'robot-head-session/1.0', 'session_id': answers['session_id'],
               'exported_at': datetime.now(timezone.utc).isoformat(),
               'created_at': answers.get('created_at'),
               'stage': answers.get('stage', 'assessment'),
               'wake_event': answers.get('wake_event'),
               'questionnaire_provenance': {key: answers.get(key) for key in (
                   'translation', 'source', 'scoring_source', 'response_options',
                   'administration_mode', 'reference_period')},
               'status': answers.get('status'), 'instrument': answers.get('instrument'),
               'reference_period': answers.get('reference_period'),
               'workflow': answers.get('workflow'), 'participant': answers.get('participant'),
               'total_questions': answers.get('total_questions'),
               'completed_count': answers.get('completed_count'),
               'questions': answers.get('questions', []), 'tasks': answers.get('tasks', []),
               'intake_questions': answers.get('intake_questions', []),
               'instrument_version': answers.get('instrument_version'),
               'validation_status': answers.get('validation_status'),
               'scoring_notice': answers.get('scoring_notice'),
               'task_reflections': answers.get('task_reflections', []),
               'task_scoring': answers.get('task_scoring'),
               'score_comparison': answers.get('score_comparison'),
               'scoring': answers.get('scoring'),
               'overall_summary': answers.get('overall_summary'), 'conversation': events,
               'unrecognized_segments': answers.get('unrecognized_segments', []),
               'full_audio': audio_manifest, 'video': video_manifest, 'media': media,
               'limitations': ['Scores describe self-reported symptoms and are not a diagnosis',
                              'Only fully answered subscales are scored; missing answers are not zero-filled',
                              'Playback transcript distinguishes planned output from submitted audio',
                              'Video is available only when the shared camera opened and the recorder captured frames']}
    atomic_json(directory / 'session_package.json', package)
    archive_path = directory / 'session_package.zip'
    if archive:
        members = [directory / name for name in ('answers.json', 'events.jsonl', 'transcript.json',
                                                  'summary.txt', 'session_package.json')]
        members.extend(directory / item['path'] for item in media)
        temporary = archive_path.with_suffix('.tmp.zip')
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3) as file:
            for member in members:
                if member.is_file() and not member.is_symlink() and member.resolve().is_relative_to(directory):
                    file.write(member, member.relative_to(directory).as_posix())
        temporary.chmod(0o600)
        temporary.replace(archive_path)
    return {'json_path': 'session_package.json',
            'archive_path': 'session_package.zip' if archive else None,
            'event_count': len(events), 'media_count': len(media)}
