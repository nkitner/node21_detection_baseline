import SimpleITK
import numpy as np

from pandas import DataFrame
from scipy.ndimage import center_of_mass, label
import torchvision
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor
import torch
from evalutils import DetectionAlgorithm
from evalutils.validators import (
    UniquePathIndicesValidator,
    UniqueImagesValidator,
)
from skimage import transform
import json
from typing import Dict
import training_utils.utils as utils
from training_utils.dataset import CXRNoduleDataset, get_transform
import os
from training_utils.train import train_one_epoch, val_metrics
import itertools
from pathlib import Path
from postprocessing import get_NonMaxSup_boxes
from torchvision.models.detection.rpn import AnchorGenerator
from torchvision.models.detection import FasterRCNN
# from engine import train_one_epoch, evaluate

'''
NODE21 template nodule detection codebase
Author: Ecem Sogancioglu
email: ecemsogancioglu@gmail.com
'''

# This parameter adapts the paths between local execution and execution in docker. You can use this flag to switch between these two modes.
# For building your docker, set this parameter to True. If False, it will run process.py locally for test purposes.
execute_in_docker = False


class Noduledetection(DetectionAlgorithm):
    def __init__(self, input_dir, output_dir, train=False, retrain=False, retest=False):
        super().__init__(
            validators=dict(
                input_image=(
                    UniqueImagesValidator(),
                    UniquePathIndicesValidator(),
                )
            ),
            input_path=Path(input_dir),
            output_file=Path(os.path.join(output_dir, 'nodules.json'))
        )

        # ------------------------------- LOAD the model here ---------------------------------
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.input_path, self.output_path = input_dir, output_dir
        print('using the device ', self.device)

        # CheXNet pretrained weights are loaded into state_dict_model
        state_dict_model = torch.load("/Volumes/ExternalHardDrive/node21_detection_baseline/Untitled/model.pth.tar", map_location=self.device)
        # DenseNet121 is set as the backbone to the FasterRCNN
        pretrained = torchvision.models.densenet121(pretrained=True)
        pretrained.load_state_dict(state_dict_model['state_dict'], strict=False)
        pretrained = pretrained.features
        pretrained.out_channels = 1024
        # Anchor points
        anchor_generator = AnchorGenerator(
            sizes=((32,64, 128, 256),),
            aspect_ratios=((0.25,0.5, 1.0, 2.0),))
        self.model = FasterRCNN(pretrained, 2, rpn_anchor_generator=anchor_generator)

        if not (train or retest):
            # retrain or test phase
            print('loading the model.pth file :')
            self.model.load_state_dict(
                torch.load(
                    Path("/opt/algorithm/model.pth") if execute_in_docker else Path("/Volumes/ExternalHardDrive/node21_detection_baseline/Untitled/model_retrained_anch.pth"),
                    map_location=self.device,
                ), strict=False
            )

        if retest:
            print('loading the retrained model_retrained.pth file')
            self.model.load_state_dict(
                torch.load(
                    Path(os.path.join(self.input_path, '/Volumes/ExternalHardDrive/node21_detection_baseline/Untitled/model_retrained_anch.pth')),
                    map_location=self.device,
                ), strict = False
            )

        self.model.to(self.device)

    def save(self):
        with open(str(self._output_file), "w") as f:
            for i in range(len(self._case_results)):
                json.dump(self._case_results[i], f)

    # TODO: Copy this function for your processor as well!
    def process_case(self, idx, case):
        '''
        Read the input, perform model prediction and return the results.
        The returned value will be saved as nodules.json by evalutils.
        process_case method of evalutils
        (https://github.com/comic/evalutils/blob/fd791e0f1715d78b3766ac613371c447607e411d/evalutils/evalutils.py#L225)
        is overwritten here, so that it directly returns the predictions without changing the format.

        '''
        # Load and test the image for this case
        input_image, input_image_file_path = self._load_input_image(case=case)

        # Detect and score candidates
        scored_candidates = self.predict(input_image=input_image)

        # Write resulting candidates to nodules.json for this case
        return scored_candidates

    # --------------------Write your retrain function here ------------
    def train(self, num_epochs=1):
        '''
        input_dir: Input directory containing all the images to train with
        output_dir: output_dir to write model to.
        num_epochs: Number of epochs for training the algorithm.
        '''
        # Implementation of the pytorch model and training functions is based on pytorch tutorial: https://pytorch.org/tutorials/intermediate/torchvision_tutorial.html

        # create training dataset and defined transformations
        self.model.train()
        input_dir_training = "/Volumes/ExternalHardDrive/CISC881/dataset_node21/cxr_images/proccessed_data/training"
        input_dir_testing = "/Volumes/ExternalHardDrive/CISC881/dataset_node21/cxr_images/proccessed_data/testing"
        dataset = CXRNoduleDataset(input_dir_training, os.path.join(input_dir_training, 'metadata.csv'), get_transform(train=True))
        dataset_test = CXRNoduleDataset(input_dir_testing, os.path.join(input_dir_testing, 'metadata.csv'), get_transform(train=False))

        print('training starts ')
        # define training and validation data loaders
        data_loader = torch.utils.data.DataLoader(
            dataset, batch_size=2, shuffle=True, num_workers=4,
            collate_fn=utils.collate_fn)
        data_loader_test = torch.utils.data.DataLoader(
            dataset_test, batch_size=1, shuffle=False, num_workers=4,
            collate_fn=utils.collate_fn)

        # construct an optimizer
        params = [p for p in self.model.parameters() if p.requires_grad]
        optimizer = torch.optim.SGD(params, lr=0.005,
                                    momentum=0.9, weight_decay=0.0005)
        # and a learning rate scheduler
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer,
                                                       step_size=3,
                                                       gamma=0.1)
        for epoch in range(num_epochs):
            train_one_epoch(self.model, optimizer, data_loader, data_loader_test, self.device, epoch, print_freq=10)
            # update the learning rate
            lr_scheduler.step()
            print('epoch ', str(epoch), ' is running')
            # evaluate on the test dataset
            print("The mean iou for this epoch is: {}".format(val_metrics(self.model, data_loader_test, self.device)))
            # IMPORTANT: save retrained version frequently.
            print('saving the model')
            torch.save(self.model.state_dict(), os.path.join(self.output_path, 'model_retrained.pth'))

    def format_to_GC(self, np_prediction, spacing):
        '''
        Convenient function returns detection prediction in required grand-challenge format.
        See:
        https://comic.github.io/grandchallenge.org/components.html#grandchallenge.components.models.InterfaceKind.interface_type_annotation


        np_prediction: dictionary with keys boxes and scores.
        np_prediction[boxes] holds coordinates in the format as x1,y1,x2,y2
        spacing :  pixel spacing for x and y coordinates.

        return:
        a Dict in line with grand-challenge.org format.
        '''
        # For the test set, we expect the coordinates in millimeters.
        # this transformation ensures that the pixel coordinates are transformed to mm.
        # and boxes coordinates saved according to grand challenge ordering.
        x_y_spacing = [spacing[0], spacing[1], spacing[0], spacing[1]]
        boxes = []
        for i, bb in enumerate(np_prediction['boxes']):
            bb = np.float64(bb)
            box = {}
            box['corners'] = []
            x_min, y_min, x_max, y_max = bb * x_y_spacing
            x_min, y_min, x_max, y_max = round(x_min, 2), round(y_min, 2), round(x_max, 2), round(y_max, 2)
            bottom_left = [x_min, y_min, np_prediction['slice'][i]]
            bottom_right = [x_max, y_min, np_prediction['slice'][i]]
            top_left = [x_min, y_max, np_prediction['slice'][i]]
            top_right = [x_max, y_max, np_prediction['slice'][i]]
            # box['corners'].extend([top_right, top_left, bottom_left, bottom_right])
            box['corners'].extend(bb)
            box['probability'] = round(float(np_prediction['scores'][i]), 2)
            boxes.append(box)

        return dict(type="Multiple 2D bounding boxes", boxes=boxes, version={"major": 1, "minor": 0})

    def merge_dict(self, results):
        merged_d = {}
        for k in results[0].keys():
            merged_d[k] = list(itertools.chain(*[d[k] for d in results]))
        return merged_d

    def predict(self, input_image):
        self.model.eval()

        image_data = SimpleITK.GetArrayFromImage(input_image)
        spacing = input_image.GetSpacing()
        image_data = np.array(image_data)

        if len(image_data.shape) == 2:
            image_data = np.expand_dims(image_data, 0)

        results = []
        # operate on 3D image (CXRs are stacked together)
        for j in range(len(image_data)):
            # Pre-process the image
            image = image_data[j, :, :]
            # The range should be from 0 to 1.
            image = image.astype(np.float32) / np.max(image)  # normalize
            image = np.expand_dims(image, axis=0)
            tensor_image = torch.from_numpy(image).to(self.device)  # .reshape(1, 1024, 1024)
            with torch.no_grad():
                prediction = self.model([tensor_image.to(self.device)])

            prediction = [get_NonMaxSup_boxes(prediction[0])]
            # convert predictions from tensor to numpy array.
            np_prediction = {str(key): [i.cpu().numpy() for i in val]
                             for key, val in prediction[0].items()}
            np_prediction['slice'] = len(np_prediction['boxes']) * [j]
            results.append(np_prediction)

        predictions = self.merge_dict(results)
        data = self.format_to_GC(predictions, spacing)
        print(data)
        return data


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog='process.py',
        description=
        'Reads all images from an input directory and produces '
        'results in an output directory')

    parser.add_argument('input_dir', help="input directory to process")
    parser.add_argument('output_dir', help="output directory generate result files in")
    parser.add_argument('--train', action='store_true', help="Algorithm on train mode.")
    parser.add_argument('--retrain', action='store_true', help="Algorithm on retrain mode (loading previous weights).")
    parser.add_argument('--retest', action='store_true', help="Algorithm on evaluate mode after retraining.")

    parsed_args = parser.parse_args()
    if (parsed_args.train or parsed_args.retrain):  # train mode: retrain or train
        Noduledetection(parsed_args.input_dir, parsed_args.output_dir, parsed_args.train, parsed_args.retrain,
                        parsed_args.retest).train()
    else:  # test mode (test or retest)
        print(parsed_args.input_dir)
        Noduledetection(parsed_args.input_dir, parsed_args.output_dir, retest=parsed_args.retest).process()

