"""
YoutubeVIS data loader
"""
from pathlib import Path

import torch
import torch.utils.data
import torchvision
from pycocotools.ytvos import YTVOS
from pycocotools.ytvoseval import YTVOSeval
import datasets.transforms as T
# import transforms as T
from pycocotools import mask as coco_mask
import os
from PIL import Image
import random
from random import randint
import cv2
import random

import json

import numpy as np

import scipy.io.wavfile as wav
from bert_embedding import BertEmbedding
import clip


class YTVOSDataset:
    def __init__(self, img_folder, mask_folder, ann_file, exp_file, vocab_path, transforms, return_masks, num_frames):
        self.img_folder = img_folder
        self.mask_folder = mask_folder
        self.ann_file = ann_file
        self.exp_file = exp_file  # 表达 
        self.audio_file = 'data/audio_raw'
        self._transforms = transforms
        self.return_masks = return_masks
        self.num_frames = num_frames
        self.prepare = ConvertCocoPolysToMask(return_masks)
        self.ytvos = YTVOS(ann_file)
        # self.cat_ids = self.ytvos.getCatIds()  # 0~40
        self.vid_ids = self.ytvos.getVidIds()  # 1~2238
        self.vid_infos = []
        self.exp_infos = load_expressions(exp_file)  # 表达
        self.bert_embedding = BertEmbedding()
        self.clip_preprocess = clip.load("RN50")[1]
        # all_query = set()
        self.all_query = []
        for i in self.vid_ids:
            info = self.ytvos.loadVids([i])[0]
            info['filenames'] = info['file_names']
            self.vid_infos.append(info)
        self.img_ids = []
        for idx, vid_info in enumerate(self.vid_infos):
            filename = vid_info['file_names'][0].split('/')[0]
            exps = self.exp_infos[filename]['expressions']
            for frame_id in range(len(vid_info['filenames'])):
                for exp_id in range(len(exps)):
                    self.img_ids.append((idx, frame_id, exp_id))
                    # all_query.add(exps[exp_id]['exp'])
                    if frame_id == 0:
                        self.all_query.append(exps[exp_id]['exp'])
        self.extract_query = {}
        for i in range(len(self.img_ids)):
            numbers = random.sample(range(0, len(self.all_query)), 10)
            self.extract_query[i] = numbers

    def __len__(self):
        return len(self.img_ids)

    def __getitem__(self, idx):
        vid, frame_id, exp_id = self.img_ids[idx]
        vid_id = self.vid_infos[vid]['id']
        img = []
        vid_len = len(self.vid_infos[vid]['file_names'])
        inds = list(range(self.num_frames))
        inds = [i%vid_len for i in inds][::-1]
        # if random 
        # random.shuffle(inds)

        filename = self.vid_infos[vid]['file_names'][0].split('/')[0]

        # bert
        expressions = []
        # expressions.append(np.zeros((7, 768)))
        # text_clip = None
        exps = self.exp_infos[filename]['expressions']
        expression = exps[exp_id]['exp']
        obj_id = int(exps[exp_id]['obj_id'])
        # text_clip = clip.tokenize([expression])
        expressions.append(expression)
        numbers = self.extract_query[idx]
        for i in range(10):
            query = self.all_query[numbers[i]]
            expressions.append(query)
        text_clip = clip.tokenize(expressions)
        results = self.bert_embedding(expressions)
        expressions = [np.asarray(result[1]) for result in results]

        for j in range(self.num_frames):
            img_path = os.path.join(str(self.img_folder), self.vid_infos[vid]['file_names'][frame_id-inds[j]])
        #     mask_path = os.path.join(str(self.mask_folder), self.vid_infos[vid]['file_names'][frame_id-inds[j]][:-3]+'png')
            img.append(Image.open(img_path).convert('RGB'))

        img_clip = torch.stack([self.clip_preprocess(im) for im in img])

        ann_ids = self.ytvos.getAnnIds(vidIds=[vid_id])
        if obj_id > len(ann_ids):
            # ann_ids = [ann_ids[-1]]
            print('--------------------------', filename, obj_id)
        ann_ids = [ann_ids[obj_id-1]]

        target = self.ytvos.loadAnns(ann_ids)
        target = {'image_id': idx, 'video_id': vid, 'frame_id': frame_id, 'annotations': target}
        target = self.prepare(img[0], target, inds, self.num_frames)
        if self._transforms is not None:
            img, target = self._transforms(img, target)
        
        return torch.cat(img,dim=0), expressions, target, (img_clip, text_clip)


def load_expressions(exp_file):
    with open(exp_file) as f:
        videos = json.load(f)['videos']
    exp_infos = {}
    for k, v in videos.items():
        exps = v['expressions']
        exp_list = []
        for exp in exps.values():
            exp_list.append(exp)
        exp_infos[k] = {"expressions": exp_list, "frames": v['frames']}
    return exp_infos

def convert_coco_poly_to_mask(segmentations, height, width):
    masks = []
    for polygons in segmentations:
        if not polygons:
            mask = torch.zeros((height,width), dtype=torch.uint8)
        else:
            rles = coco_mask.frPyObjects(polygons, height, width)
            mask = coco_mask.decode(rles)
            if len(mask.shape) < 3:
                mask = mask[..., None]
            mask = torch.as_tensor(mask, dtype=torch.uint8)
            mask = mask.any(dim=2)
        masks.append(mask)
    if masks:
        masks = torch.stack(masks, dim=0)
    else:
        masks = torch.zeros((0, height, width), dtype=torch.uint8)
    return masks


class ConvertCocoPolysToMask(object):
    def __init__(self, return_masks=False):
        self.return_masks = return_masks

    def __call__(self, image, target, inds, num_frames):
        w, h = image.size
        image_id = target["image_id"]
        frame_id = target['frame_id']
        image_id = torch.tensor([image_id])

        anno = target["annotations"]
        anno = [obj for obj in anno if 'iscrowd' not in obj or obj['iscrowd'] == 0]
        boxes = []
        classes = []
        segmentations = []
        area = []
        iscrowd = []
        valid = []
        # add valid flag for bboxes
        for i, ann in enumerate(anno):
            for j in range(num_frames):
                bbox = ann['bboxes'][frame_id-inds[j]]
                areas = ann['areas'][frame_id-inds[j]]
                segm = ann['segmentations'][frame_id-inds[j]]
                clas = ann["category_id"]
                # for empty boxes
                if bbox is None:
                    bbox = [0,0,0,0]
                    areas = 0
                    valid.append(0)
                    clas = 0
                else:
                    valid.append(1)
                crowd = ann["iscrowd"] if "iscrowd" in ann else 0
                boxes.append(bbox)
                area.append(areas)
                segmentations.append(segm)
                classes.append(clas)
                iscrowd.append(crowd)
        # guard against no boxes via resizing
        boxes = torch.as_tensor(boxes, dtype=torch.float32).reshape(-1, 4)
        boxes[:, 2:] += boxes[:, :2]
        boxes[:, 0::2].clamp_(min=0, max=w)
        boxes[:, 1::2].clamp_(min=0, max=h)
        classes = torch.tensor(classes, dtype=torch.int64)
        if self.return_masks:
            masks = convert_coco_poly_to_mask(segmentations, h, w)
        target = {}
        target["boxes"] = boxes
        target["labels"] = classes
        if self.return_masks:
            target["masks"] = masks
        target["image_id"] = image_id

        # for conversion to coco api
        area = torch.tensor(area) 
        iscrowd = torch.tensor(iscrowd)
        target["valid"] = torch.tensor(valid)
        target["area"] = area
        target["iscrowd"] = iscrowd
        target["orig_size"] = torch.as_tensor([int(h), int(w)])
        target["size"] = torch.as_tensor([int(h), int(w)])
        return  target


def make_coco_transforms(image_set):

    normalize = T.Compose([
        T.ToTensor(),
        T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    ])

    scales = [480, 512, 544, 576, 608, 640, 672, 704, 736, 768]

    if image_set == 'train':
        return T.Compose([
            T.RandomHorizontalFlip(),
            T.RandomResize(scales, max_size=800),
            T.PhotometricDistort(),
            T.Compose([
                     T.RandomResize([400, 500, 600]),
                     T.RandomSizeCrop(384, 600),
                     # To suit the GPU memory the scale might be different
                     T.RandomResize([300], max_size=540),#for r50
                     #T.RandomResize([280], max_size=504),#for r101
            ]),
            normalize,
        ])

    if image_set == 'val':
        return T.Compose([
            T.RandomResize([360], max_size=640),
            normalize,
        ])

    raise ValueError(f'unknown {image_set}')


def build(image_set, args):
    root = Path(args.ytvos_path)
    assert root.exists(), f'provided YTVOS path {root} does not exist'
    mode = 'instances'
 #   PATHS = {
 #       "train": (root / "train/JPEGImages", root / "annotations" / f'{mode}_train_sub.json'),
 #       "val": (root / "valid/JPEGImages", root / "annotations" / f'{mode}_val_sub.json'),
 #   }
    PATHS = {
        "train": (root / "train/JPEGImages", root / "train/Annotations", root /  f'ann/{mode}_train_sub.json', root / "meta_expressions/train/meta_expressions.json", root / "vocab"),
        "val": (root / "valid/JPEGImages", root /  f'ann/{mode}_valid_sub.json'),
    }
    img_folder, mask_folder, ann_file, exp_file, vocab_path = PATHS[image_set]
    dataset = YTVOSDataset(img_folder, mask_folder, ann_file, exp_file, vocab_path, transforms=make_coco_transforms(image_set), return_masks=args.masks, num_frames = args.num_frames)
    return dataset
