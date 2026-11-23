import unittest
import numpy as np

from orin_voice.yolo_head import letterbox, decode_detections, select_best, annotate, YoloHeadDetector


class YoloHeadTests(unittest.TestCase):
    def test_selects_confidence_not_area_and_rejects_weak(self):
        large={'bbox':[0,0,300,300],'confidence':.8}
        small={'bbox':[100,100,30,30],'confidence':.95}
        self.assertIs(select_best([large,small]),small)
        self.assertIsNone(select_best([{'bbox':[0,0,10,10],'confidence':.2}]))
        self.assertIsNone(select_best([]))

    def test_letterbox_inverse_coordinates(self):
        image=np.zeros((480,640,3),dtype=np.uint8)
        canvas,scale,left,top=letterbox(image)
        self.assertEqual(canvas.shape,(640,640,3))
        self.assertEqual((scale,left,top),(1.,0,80))
        output=np.array([[[320],[320],[100],[200],[.9]]],dtype=np.float32)
        result=decode_detections(output,image.shape,scale,left,top)
        self.assertEqual(result[0]['bbox'],[270,140,100,200])
        self.assertAlmostEqual(result[0]['confidence'],.9,places=6)

    def test_nms_overlaps_and_clipped_coordinates(self):
        rows=np.array([[320,320,100,100,.9],[321,320,100,100,.8],[-20,10,100,50,.6]])
        result=decode_detections(rows.T[np.newaxis],(640,640,3),1.,0,0)
        self.assertEqual(len(result),2)
        self.assertGreater(result[0]['confidence'],result[1]['confidence'])
        self.assertEqual(result[1]['bbox'][:2],[0,0])

    def test_empty_bad_shapes_and_nonfinite_values(self):
        self.assertEqual(decode_detections(np.zeros((1,5,10)),(480,640,3),1.,0,80),[])
        with self.assertRaises(RuntimeError):
            decode_detections(np.zeros((1,20,8400)),(480,640,3),1.,0,80)
        rows=np.array([[320,320,100,100,np.nan],[320,320,100,100,2.0]])
        self.assertEqual(decode_detections(rows.T[np.newaxis],(480,640,3),1.,0,80),[])

    def test_annotation_does_not_mutate_shared_frame_or_draw_two_boxes(self):
        image=np.zeros((200,300,3),dtype=np.uint8)
        large={'bbox':[0,0,190,190],'confidence':.7}
        small={'bbox':[220,80,40,40],'confidence':.9}
        output=annotate(image,[large,small])
        self.assertEqual(int(image.sum()),0)
        self.assertGreater(int(output.sum()),0)
        self.assertEqual(int(output[100,0].sum()),0)
        np.testing.assert_array_equal(annotate(image,None),image)

    def test_missing_model_fails_without_fake_detection(self):
        with self.assertRaises(FileNotFoundError):
            YoloHeadDetector({'model_path':'/tmp/does-not-exist-head-model.onnx'})
