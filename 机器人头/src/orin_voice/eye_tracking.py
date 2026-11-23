"""The selected YOLO target from the displayed frame drives the real eye axes."""
import copy
import threading
import time


class EyeTracker:
    def __init__(self, camera, hardware):
        self.camera, self.hardware = camera, hardware
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self._state = {'enabled':False,'phase':'stopped','error':None,'target':None,'frames':0}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.status()
            if getattr(self.camera,'detector_kind',None) != 'yolo':
                raise ValueError('请先启用 YOLO 人脸检测，再启动眼球跟随')
            self.stop_event = threading.Event()
            self._state.update(enabled=True,phase='connecting',error=None,target=None,frames=0)
            self.camera.start()
            self.hardware.start()
            self.thread = threading.Thread(target=self._run,name='yolo-eye-tracking',daemon=True)
            self.thread.start()
        return self.status()

    def _update(self, **values):
        with self.lock:
            self._state.update(values)

    def _run(self):
        sequence = -1
        try:
            while not self.stop_event.is_set():
                hardware = self.hardware.status()
                if hardware.get('error'):
                    raise RuntimeError(hardware['error'])
                frame = self.camera.wait_frame(sequence,timeout=.25)
                if frame is None:
                    camera = self.camera.status()
                    if camera.get('error') or camera.get('detector_error'):
                        raise RuntimeError(camera.get('error') or camera.get('detector_error'))
                    if camera.get('phase') == 'stopped':
                        raise RuntimeError('摄像头已停止，眼球跟随已停止')
                    continue
                sequence = frame.sequence
                target = frame.target
                if target:
                    height,width = frame.frame.shape[:2]
                    x,y,w,h = target['bbox']
                    nx = max(-1.,min(1.,(x+w/2)/width*2-1))
                    ny = max(-1.,min(1.,(y+h/2)/height*2-1))
                    self.hardware.track_target(nx,ny,visible=True)
                    self._update(phase='tracking' if hardware.get('motion_enabled') else 'connecting',
                                 target={**target,'x':nx,'y':ny,'sequence':sequence},
                                 frames=self._state['frames']+1)
                else:
                    self.hardware.track_target(0.,0.,visible=False)
                    self._update(phase='waiting_face',target=None,frames=self._state['frames']+1)
        except Exception as error:
            self._update(phase='error',error=str(error))
        finally:
            self.hardware.track_target(0.,0.,visible=False)
            self.hardware.close()
            with self.lock:
                self._state.update(enabled=False,phase='error' if self._state['error'] else 'stopped')

    def status(self):
        with self.lock:
            result = copy.deepcopy(self._state)
        result['hardware'] = self.hardware.status()
        return result

    def is_alive(self):
        return bool(self.thread and self.thread.is_alive())

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=7)
        if self.thread and self.thread.is_alive():
            self._update(error='眼球跟随仍在停止，请稍候')
        return self.status()
