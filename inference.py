'''
Inference code for CVMN
Modified from DETR (https://github.com/facebookresearch/detr)
'''
import argparse
import datetime
import json
import random
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader, DistributedSampler

import datasets
import util.misc as utils
from datasets import build_dataset, get_coco_api_from_dataset
from models import build_model
import torchvision.transforms as T
import matplotlib.pyplot as plt
import os
from PIL import Image
import math
import torch.nn.functional as F
import json
from scipy.optimize import linear_sum_assignment
import pycocotools.mask as mask_util

from util.misc import nested_tensor_from_exp

import yaml

# from .tokenizer import Tokenizer
import numpy as np

from gensim.models import KeyedVectors
from bert_embedding import BertEmbedding
import csv
import h5py
import cv2
import clip

# os.environ["CUDA_VISIBLE_DEVICES"] = '1'


def get_args_parser():
    parser = argparse.ArgumentParser('Set transformer detector', add_help=False)
    parser.add_argument('--lr', default=1e-4, type=float)
    parser.add_argument('--lr_backbone', default=1e-5, type=float)
    parser.add_argument('--batch_size', default=2, type=int)
    parser.add_argument('--weight_decay', default=1e-4, type=float)
    parser.add_argument('--epochs', default=150, type=int)
    parser.add_argument('--lr_drop', default=100, type=int)
    parser.add_argument('--clip_max_norm', default=0.1, type=float,
                        help='gradient clipping max norm')

    # Model parameters
    parser.add_argument('--model_path', type=str, default='output/checkpoint.pth',
                        help="Path to the model weights.")
    # * Backbone
    parser.add_argument('--backbone', default='resnet50', type=str,
                        help="Name of the convolutional backbone to use")
    parser.add_argument('--dilation', action='store_true',
                        help="If true, we replace stride with dilation in the last convolutional block (DC5)")
    parser.add_argument('--position_embedding', default='sine', type=str, choices=('sine', 'learned'),
                        help="Type of positional embedding to use on top of the image features")

    # * Transformer
    parser.add_argument('--enc_layers', default=4, type=int,
                        help="Number of encoding layers in the transformer")
    parser.add_argument('--dec_layers', default=4, type=int,
                        help="Number of decoding layers in the transformer")
    parser.add_argument('--dim_feedforward', default=2048, type=int,
                        help="Intermediate size of the feedforward layers in the transformer blocks")
    parser.add_argument('--hidden_dim', default=384, type=int,
                        help="Size of the embeddings (dimension of the transformer)")
    parser.add_argument('--dropout', default=0.1, type=float,
                        help="Dropout applied in the transformer")
    parser.add_argument('--nheads', default=8, type=int,
                        help="Number of attention heads inside the transformer's attentions")
    parser.add_argument('--num_frames', default=36, type=int,
                        help="Number of frames")
    parser.add_argument('--num_ins', default=1, type=int,
                        help="Number of instances")
    parser.add_argument('--num_queries', default=36, type=int,
                        help="Number of query slots")
    parser.add_argument('--pre_norm', action='store_true')

    # * Segmentation
    parser.add_argument('--masks', action='store_false',
                        help="Train segmentation head if the flag is provided")

    # Loss
    parser.add_argument('--no_aux_loss', dest='aux_loss', action='store_false',
                        help="Disables auxiliary decoding losses (loss at each layer)")
    # * Matcher
    parser.add_argument('--set_cost_class', default=1, type=float,
                        help="Class coefficient in the matching cost")
    parser.add_argument('--set_cost_bbox', default=5, type=float,
                        help="L1 box coefficient in the matching cost")
    parser.add_argument('--set_cost_giou', default=2, type=float,
                        help="giou box coefficient in the matching cost")
    # * Loss coefficients
    parser.add_argument('--mask_loss_coef', default=1, type=float)
    parser.add_argument('--dice_loss_coef', default=1, type=float)
    parser.add_argument('--bbox_loss_coef', default=5, type=float)
    parser.add_argument('--giou_loss_coef', default=2, type=float)
    parser.add_argument('--eos_coef', default=0.1, type=float,
                        help="Relative classification weight of the no-object class")

    # dataset parameters
    parser.add_argument('--img_path', default='data/rvos/train/JPEGImages/')
    parser.add_argument('--ann_path', default='data/rvos/ann/instances_test_sub.json')
    parser.add_argument('--save_path', default='')
    parser.add_argument('--dataset_file', default='ytvos')
    parser.add_argument('--coco_path', type=str)
    parser.add_argument('--coco_panoptic_path', type=str)
    parser.add_argument('--remove_difficult', action='store_true')

    parser.add_argument('--output_dir', default='output_ytvos',
                        help='path where to save, empty for no saving')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=42, type=int)
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    #parser.add_argument('--eval', action='store_true')
    parser.add_argument('--eval', action='store_false')
    parser.add_argument('--num_workers', default=0, type=int)

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')
    return parser

CLASSES=['person','giant_panda','lizard','parrot','skateboard','sedan','ape',
         'dog','snake','monkey','hand','rabbit','duck','cat','cow','fish',
         'train','horse','turtle','bear','motorbike','giraffe','leopard',
         'fox','deer','owl','surfboard','airplane','truck','zebra','tiger',
         'elephant','snowboard','boat','shark','mouse','frog','eagle','earless_seal',
         'tennis_racket']
COLORS = [[0.000, 0.447, 0.741], [0.850, 0.325, 0.098], [0.929, 0.694, 0.125],
          [0.494, 0.184, 0.556], [0.466, 0.674, 0.188], [0.301, 0.745, 0.933],
          [0.494, 0.000, 0.556], [0.494, 0.000, 0.000], [0.000, 0.745, 0.000],
          [0.700, 0.300, 0.600]]
transform = T.Compose([
    T.Resize(300),
    T.ToTensor(),
    T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
])


# for output bounding box post-processing
def box_cxcywh_to_xyxy(x):
    x_c, y_c, w, h = x.unbind(1)
    b = [(x_c - 0.5 * w), (y_c - 0.5 * h),
         (x_c + 0.5 * w), (y_c + 0.5 * h)]
    return torch.stack(b, dim=1)

def rescale_bboxes(out_bbox, size):
    img_w, img_h = size
    b = box_cxcywh_to_xyxy(out_bbox)
    b = b.cpu() * torch.tensor([img_w, img_h, img_w, img_h], dtype=torch.float32)
    return b


def computeIoU(pred_seg, gd_seg):
    I = np.sum(np.logical_and(pred_seg, gd_seg))
    U = np.sum(np.logical_or(pred_seg, gd_seg))

    return I, U


def main(args):

    device = torch.device(args.device)
    # device = torch.device('cpu')

    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    num_frames = args.num_frames
    num_ins = args.num_ins

    # evaluation variables
    cum_I, cum_U = 0, 0
    eval_seg_iou_list = [.5, .6, .7, .8, .9]
    seg_correct = np.zeros(len(eval_seg_iou_list), dtype=np.int32)
    seg_total = 0
    mean_IoU = []
    header = 'Test:'

    with torch.no_grad():
        model, criterion, postprocessors = build_model(args)
        model.to(device)

        state_dict = torch.load(args.model_path)['model']
        # print(state_dict.keys())
        model.load_state_dict(state_dict, strict=False)

        paths = {
            "videoset_path": "data/a2d/Release/videoset.csv",
            "annotation_path": "data/a2d/Release/Annotations",
            "sample_path": "data/a2d/a2d_annotation_info.txt",
        }
        word2vec = KeyedVectors.load_word2vec_format(paths['vocab_path'], binary=True)
        col_path = os.path.join(paths['annotation_path'], 'col')
        max_num_words = paths['max_num_words']
        # import pickle
        # with open('cnt.pkl', 'rb') as fp:
        #     id2idx = pickle.load(fp)
        bert_embedding = BertEmbedding()
        selector, preprocess = clip.load("RN50", device=device)
        test_videos = {}
        with open(paths['videoset_path'], newline='') as fp:
            reader = csv.reader(fp, delimiter=',')
            for row in reader:
                frame_idx = list(map(lambda x: int(x[:-4]) - 1, os.listdir(os.path.join(col_path, row[0]))))
                frame_idx = sorted(frame_idx)
                video_info = {
                    'label': int(row[1]),
                    'timestamps': [row[2], row[3]],
                    'size': [int(row[4]), int(row[5])],  # [height, width]
                    'num_frames': int(row[6]),
                    'num_annotations': int(row[7]),
                    'frame_idx': frame_idx,
                }
                if int(row[8]) == 1:
                    test_videos[row[0]] = video_info

        test_samples = []
        test_videos_set = set()
        with open(paths['sample_path'], newline='') as fp:
            reader = csv.DictReader(fp)
            from collections import defaultdict
            video2frame = defaultdict(list)
            rows = []
            for row in reader:
                rows.append(row)
                video2frame[(row['video_id'], row['query'])].append(row['frame_idx'])
            for row in rows:
                if row['video_id'] in test_videos:
                    test_samples.append([row['video_id'], row['instance_id'], row['frame_idx'], row['query']])
                    test_videos_set.add(row['video_id'])
        
        iou = 0
        print('test:', len(test_samples))
        for i in range(len(test_samples)):
            video_id, instance_id, frame_idx, query = test_samples[i]
            query = query.lower()
            frame_idx = int(frame_idx)
            h5_path = os.path.join('../lzj/data/a2d/a2d_annotation_with_instances', video_id, '%05d.h5' % (frame_idx + 1))
            if not os.path.exists(h5_path):
                h5_path = os.path.join('../lzj/data/a2d/a2d_annotation_with_instances', video_id, '%05d.h5' % (24 + 1))
            frame_path = os.path.join('../lzj/data/a2d/Release/pngs320H', video_id)
            frames = list(map(lambda x: os.path.join(frame_path, x), sorted(os.listdir(frame_path))))   
            assert len(frames) == test_videos[video_id]['num_frames']
            all_frames = []
            mid_frame = (args.num_frames-1)//2
            for j in range(args.num_frames):
                all_frames.append(frame_idx-mid_frame+j)
            for j in range(len(all_frames)):
                if all_frames[j] < 0:
                    all_frames[j] = 0
                elif all_frames[j] >= len(frames):
                    all_frames[j] = len(frames) - 1
            all_frames = np.asarray(frames)[all_frames]
            img_set = []
            for j in all_frames:
                im = Image.open(j)
                img_set.append(transform(im).unsqueeze(0).cuda())
            img=torch.cat(img_set,0)

            exp = bert_embedding([query])
            exp = np.asarray(exp[0][1])
            exp = nested_tensor_from_exp([exp]).to(device)
            exp = exp.tensors

            outputs = model(img, exp)
            masks = outputs['pred_masks'][0][mid_frame]
            pred_masks =F.interpolate(masks.reshape(1,num_ins,masks.shape[-2],masks.shape[-1]),(im.size[1],im.size[0]),mode="bilinear").sigmoid().cpu().detach().numpy()>0.5

            with h5py.File(h5_path, mode='r') as fp:
                instance = np.asarray(fp['instance'])
                all_masks = np.asarray(fp['reMask'])
                if len(all_masks.shape) == 3 and instance.shape[0] != all_masks.shape[0]:
                    print(video_id, frame_idx + 1, instance.shape, all_masks.shape)
                assert len(all_masks.shape) == 2 or len(all_masks.shape) == 3
                if len(all_masks.shape) == 2:
                    mask = all_masks[np.newaxis]
                else:
                    instance_id = int(instance_id)
                    idx = np.where(instance == instance_id)[0][0]
                    mask = all_masks[idx]
                    mask = mask[np.newaxis]
                assert len(mask.shape) == 3
                assert mask.shape[0] > 0
                fine_gt_mask = np.transpose(np.asarray(mask), (0, 2, 1))[0]
            
            I, U = computeIoU(pred_masks[0][0], fine_gt_mask)
            if U == 0:
                this_iou = 0.0
            else:
                this_iou = I*1.0/U
            mean_IoU.append(this_iou)
            cum_I += I
            cum_U += U
            for n_eval_iou in range(len(eval_seg_iou_list)):
                eval_seg_iou = eval_seg_iou_list[n_eval_iou]
                seg_correct[n_eval_iou] += (this_iou >= eval_seg_iou)
            seg_total += 1

        print(args.model_path)

        mean_IoU = np.array(mean_IoU)
        mIoU = np.mean(mean_IoU)
        print('Final results:')
        print('Mean IoU is %.2f\n' % (mIoU*100.))
        results_str = ''
        for n_eval_iou in range(len(eval_seg_iou_list)):
            results_str += '    precision@%s = %.2f\n' % \
                        (str(eval_seg_iou_list[n_eval_iou]), seg_correct[n_eval_iou] * 100. / seg_total)
        results_str += '    overall IoU = %.2f\n' % (cum_I * 100. / cum_U)
        print(results_str)


if __name__ == '__main__':
    parser = argparse.ArgumentParser('CVMN inference script', parents=[get_args_parser()])
    args = parser.parse_args()
    main(args)
