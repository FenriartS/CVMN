"""
Instance Sequence Segmentation 
Modified from DETR (https://github.com/facebookresearch/detr)
"""
import io
from collections import defaultdict
from turtle import forward
from typing import List, Optional
from numpy import argmax

import torch
from torch._C import device
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from PIL import Image
from .dcn.deform_conv import DeformConv

import util.box_ops as box_ops
from util.misc import NestedTensor, interpolate, nested_tensor_from_exp, nested_tensor_from_tensor_list

import torchvision.transforms as T

try:
    from panopticapi.utils import id2rgb, rgb2id
except ImportError:
    pass
import time
BN_MOMENTUM = 0.1

def conv3x3(in_planes, out_planes, stride=1):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)

class BasicBlock(nn.Module):
    expansion = 1
    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(BasicBlock, self).__init__()
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.bn1 = nn.BatchNorm2d(planes, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(planes, planes)
        self.bn2 = nn.BatchNorm2d(planes, momentum=BN_MOMENTUM)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x
        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)
        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)
        return out



class CVMNsegm(nn.Module):
    def __init__(self, cvmn, freeze_cvmn=False):
        super().__init__()
        self.cvmn = cvmn

        if freeze_cvmn:
            for p in self.parameters():
                p.requires_grad_(False)

        hidden_dim, nheads = cvmn.transformer.d_model, cvmn.transformer.nhead
        self.bbox_attention = MHAttentionMap(hidden_dim, hidden_dim, nheads, dropout=0.0)
        self.mask_head = MaskHeadSmallConv(hidden_dim + nheads, [1024, 512, 256], hidden_dim)
        self.insmask_head = nn.Sequential(
                                nn.Conv3d(24,12,3,padding=2,dilation=2),
                                nn.GroupNorm(4,12),
                                nn.ReLU(),
                                nn.Conv3d(12,12,3,padding=2,dilation=2),
                                nn.GroupNorm(4,12),
                                nn.ReLU(),
                                nn.Conv3d(12,12,3,padding=2,dilation=2),
                                nn.GroupNorm(4,12),
                                nn.ReLU(),
                                nn.Conv3d(12,1,1))

    def forward(self, samples: NestedTensor, expressions, selector=None, is_source=True, alpha=0):
        if not isinstance(samples, NestedTensor):
            samples = nested_tensor_from_tensor_list(samples)
        # if not isinstance(expressions, NestedTensor):
        #     expressions = nested_tensor_from_exp(expressions)
        features, pos = self.cvmn.backbone(samples)
        bs = features[-1].tensors.shape[0]
        src, mask = features[-1].decompose()  # src:36*2048*10*15   mask:36*10*15
        assert mask is not None
        src_proj = self.cvmn.input_proj(src) # 36*384*10*15
        n,c,s_h,s_w = src_proj.shape
        bs_f = bs//self.cvmn.num_frames
        src_proj = src_proj.reshape(bs_f, self.cvmn.num_frames,c, s_h, s_w).permute(0,2,1,3,4).flatten(-2)  # 1*384*36*150
        mask = mask.reshape(bs_f, self.cvmn.num_frames, s_h*s_w)  # 1*36*150
        pos = pos[-1].permute(0,2,1,3,4).flatten(-2)  # 1*384*36*150  bs*c*l*dim

        # exp_tensor, exp_mask = expressions.decompose()
        exp = self.cvmn.proj_t(expressions.transpose(1, 2))
        out = {}
        
        hs, memory, fusion = self.cvmn.transformer(src_proj, mask, exp, self.cvmn.query_embed.weight, pos, [])

        # hallucinator
        memory_h = memory.mean(-1).transpose(1, 2)
        memory_h = self.cvmn.hallucinator(memory_h)

        outputs_coord = self.cvmn.bbox_embed(hs).sigmoid()
        # out = {"pred_logits": outputs_class[-1], "pred_boxes": outputs_coord[-1]}
        out["pred_boxes"] = outputs_coord[-1]
        out['memory'] = fusion[0]  # 3600*1*384
        out['fusion'] = fusion[1]
        out['memory_h'] = memory_h
        if self.cvmn.aux_loss:
            out['aux_outputs'] = [{'pred_boxes': a} for a in outputs_coord[:-1]]
        for i in range(3):
            _,c_f,h,w = features[i].tensors.shape
            features[i].tensors = features[i].tensors.reshape(bs_f, self.cvmn.num_frames, c_f, h,w)
        n_f = self.cvmn.num_queries//self.cvmn.num_frames
        if n_f == 0:
            n_f = 1
        outputs_seg_masks = []
        
        # image level processing using box attention
        for i in range(self.cvmn.num_frames):
            hs_f = hs[-1][:,i*n_f:(i+1)*n_f,:]
            memory_f = memory[:,:,i,:].reshape(bs_f, c, s_h,s_w)
            mask_f = mask[:,i,:].reshape(bs_f, s_h,s_w)
            bbox_mask_f = self.bbox_attention(hs_f, memory_f, mask=mask_f)
            seg_masks_f = self.mask_head(memory_f, bbox_mask_f, [features[2].tensors[:,i], features[1].tensors[:,i], features[0].tensors[:,i]])
            outputs_seg_masks_f = seg_masks_f.view(bs_f, n_f, 24, seg_masks_f.shape[-2], seg_masks_f.shape[-1])
            outputs_seg_masks.append(outputs_seg_masks_f)
        frame_masks = torch.cat(outputs_seg_masks,dim=0)
        outputs_seg_masks = []

        # instance level processing using 3D convolution
        for i in range(frame_masks.size(1)):
            mask_ins = frame_masks[:,i].unsqueeze(0)
            mask_ins = mask_ins.permute(0,2,1,3,4)
            outputs_seg_masks.append(self.insmask_head(mask_ins))
        outputs_seg_masks = torch.cat(outputs_seg_masks,1).squeeze(0).permute(1,0,2,3)  # 36*10*75*101
        # outputs_seg_masks = outputs_seg_masks.reshape(1,360,outputs_seg_masks.size(-2),outputs_seg_masks.size(-1))  # 1*360*75*101
        # outputs_seg_masks = outputs_seg_masks.reshape(bs_f,36,outputs_seg_masks.size(-2),outputs_seg_masks.size(-1))
        outputs_seg_masks = outputs_seg_masks.reshape(bs_f,self.cvmn.num_frames,outputs_seg_masks.size(-2),outputs_seg_masks.size(-1))
        # outputs_seg_masks = outputs_seg_masks.reshape(bs_f,1,outputs_seg_masks.size(-2),outputs_seg_masks.size(-1))
        out["pred_masks"] = outputs_seg_masks


        visual_feature = samples.tensors
        seg_mask = F.interpolate(outputs_seg_masks, size=visual_feature.shape[-2:], mode='bilinear')
        seg_mask = seg_mask.transpose(0,1)
        out["pred_interp"] = seg_mask
        visual_feature = torch.mul(visual_feature, seg_mask)
        process = T.Compose([T.Resize(size=224), T.CenterCrop(size=(224,224))])
        visual_feature1 = process(visual_feature)
        out["rec_feature"] = visual_feature1

        return out


def _expand(tensor, length: int):
    return tensor.unsqueeze(1).repeat(1, int(length), 1, 1, 1).flatten(0, 1)


class Reconstructor(nn.Module):
    def __init__(self):
        super().__init__()
        # self.rnn = nn.LSTM(input_size=384, hidden_size=384, batch_first=True)
        self.rnn_re = nn.LSTM(input_size=384, hidden_size=768, batch_first=True)
        self.conv = nn.Conv1d(1024, 384, kernel_size=1, padding=0, bias=False)

    def forward(self, s, v):
        s = s[:, 0, :, :]
        v = v[:, 0, :, :, :]
        s = s.flatten().sigmoid() > 0.5   # 1*36*75*76 -> 205200
        num = s.sum()
        # v = v.permute(2,0,1,3,4).flatten(-4)   # 1*36*256*75*76 -> 256*205200
        v = v.permute(1,0,2,3).flatten(-3)
        v_f = torch.mul(s, v)
        v_re = torch.cat([v_f, v], dim=0).unsqueeze(0)
        v_re = self.conv(v_re).transpose(1,2)  # 1*l*384
        # output, (h_re, c) = self.rnn_re(v_re[:,:5000,:])
        output, (h_re, c) = self.rnn_re(v_re)
        # output, h_re = self.rnn_re(v_re)
        # output, (h_x, c) =  self.rnn(exp)
        return h_re[0]

# class Reconstructor(nn.Module):
#     def __init__(self):
#         super().__init__()
#         self.maxpool1 = nn.Sequential(
#             nn.Conv2d(1024, 128, kernel_size=3,stride=1, padding=1),
#             nn.ReLU(inplace=True),
#             nn.Conv2d(128, 128, kernel_size=3,stride=1, padding=1),
#             nn.ReLU(inplace=True),
#             nn.MaxPool2d(kernel_size=4, stride=4)
#             )
        
#         self.maxpool2 = nn.Sequential(
#             nn.Conv2d(128, 128, kernel_size=3,stride=1, padding=1),
#             nn.ReLU(inplace=True),
#             nn.Conv2d(128, 768, kernel_size=3,stride=1, padding=1),
#             nn.ReLU(inplace=True),
#             nn.MaxPool2d(kernel_size=4, stride=4)
#             )
        
#         self.maxpool3 = nn.MaxPool2d(kernel_size=2)


#     def forward(self, s, v):
#         s = s[:, 0, :, :]
#         v = v[:, 0, :, :, :]
#         s = s.sigmoid() > 0.5   # 1*75*76 
#         num = s.sum()
#         # v = v.permute(2,0,1,3,4).flatten(-4)   # 1*36*256*75*76 -> 256*205200
#         v = v.permute(1,0,2,3)
#         v_f = torch.mul(s, v)
#         v_re = torch.cat([v_f, v], dim=0).permute(1,0,2,3)
#         v_re = self.maxpool1(v_re)
#         v_re = self.maxpool2(v_re)
#         v_re = self.maxpool3(v_re).squeeze()
#         return v_re.unsqueeze(0)


class MaskHeadSmallConv(nn.Module):
    """
    Simple convolutional head, using group norm.
    Upsampling is done using a FPN approach
    """

    def __init__(self, dim, fpn_dims, context_dim):
        super().__init__()

        inter_dims = [dim, context_dim // 2, context_dim // 4, context_dim // 8, context_dim // 16, context_dim // 64]
        self.lay1 = torch.nn.Conv2d(dim, dim, 3, padding=1)
        self.gn1 = torch.nn.GroupNorm(8, dim)
        self.lay2 = torch.nn.Conv2d(dim, inter_dims[1], 3, padding=1)
        self.gn2 = torch.nn.GroupNorm(8, inter_dims[1])
        self.lay3 = torch.nn.Conv2d(inter_dims[1], inter_dims[2], 3, padding=1)
        self.gn3 = torch.nn.GroupNorm(8, inter_dims[2])
        self.lay4 = torch.nn.Conv2d(inter_dims[2], inter_dims[3], 3, padding=1)
        self.gn4 = torch.nn.GroupNorm(8, inter_dims[3])
        self.gn5 = torch.nn.GroupNorm(8, inter_dims[4])
        self.conv_offset = torch.nn.Conv2d(inter_dims[3], 18, 1)#, bias=False)
        self.dcn = DeformConv(inter_dims[3],inter_dims[4], 3, padding=1)

        self.dim = dim

        self.adapter1 = torch.nn.Conv2d(fpn_dims[0], inter_dims[1], 1)
        self.adapter2 = torch.nn.Conv2d(fpn_dims[1], inter_dims[2], 1)
        self.adapter3 = torch.nn.Conv2d(fpn_dims[2], inter_dims[3], 1)

        for name, m in self.named_modules():
            if name == "conv_offset":
                nn.init.constant_(m.weight, 0)
                nn.init.constant_(m.bias, 0)
            else:
                if isinstance(m, nn.Conv2d):
                    nn.init.kaiming_uniform_(m.weight, a=1)
                    nn.init.constant_(m.bias, 0)

    def forward(self, x: Tensor, bbox_mask: Tensor, fpns: List[Tensor]):
        x = torch.cat([_expand(x, bbox_mask.shape[1]), bbox_mask.flatten(0, 1)], 1)

        x = self.lay1(x)
        x = self.gn1(x)
        x = F.relu(x)
        x = self.lay2(x)
        x = self.gn2(x)
        x = F.relu(x)

        cur_fpn = self.adapter1(fpns[0])
        if cur_fpn.size(0) != x.size(0):
            cur_fpn = _expand(cur_fpn, x.size(0) // cur_fpn.size(0))
        x = cur_fpn + F.interpolate(x, size=cur_fpn.shape[-2:], mode="nearest")
        x = self.lay3(x)
        x = self.gn3(x)
        x = F.relu(x)

        cur_fpn = self.adapter2(fpns[1])
        if cur_fpn.size(0) != x.size(0):
            cur_fpn = _expand(cur_fpn, x.size(0) // cur_fpn.size(0))
        x = cur_fpn + F.interpolate(x, size=cur_fpn.shape[-2:], mode="nearest")
        x = self.lay4(x)
        x = self.gn4(x)
        x = F.relu(x)

        cur_fpn = self.adapter3(fpns[2])
        if cur_fpn.size(0) != x.size(0):
            cur_fpn = _expand(cur_fpn, x.size(0) // cur_fpn.size(0))
        x = cur_fpn + F.interpolate(x, size=cur_fpn.shape[-2:], mode="nearest")
        # dcn for the last layer
        offset = self.conv_offset(x)
        x = self.dcn(x,offset)
        x = self.gn5(x)
        x = F.relu(x)
        return x


class MHAttentionMap(nn.Module):
    """This is a 2D attention module, which only returns the attention softmax (no multiplication by value)"""

    def __init__(self, query_dim, hidden_dim, num_heads, dropout=0.0, bias=True):
        super().__init__()
        self.num_heads = num_heads
        self.hidden_dim = hidden_dim
        self.dropout = nn.Dropout(dropout)

        self.q_linear = nn.Linear(query_dim, hidden_dim, bias=bias)
        self.k_linear = nn.Linear(query_dim, hidden_dim, bias=bias)

        nn.init.zeros_(self.k_linear.bias)
        nn.init.zeros_(self.q_linear.bias)
        nn.init.xavier_uniform_(self.k_linear.weight)
        nn.init.xavier_uniform_(self.q_linear.weight)
        self.normalize_fact = float(hidden_dim / self.num_heads) ** -0.5

    def forward(self, q, k, mask: Optional[Tensor] = None):
        q = self.q_linear(q)
        k = F.conv2d(k, self.k_linear.weight.unsqueeze(-1).unsqueeze(-1), self.k_linear.bias)
        qh = q.view(q.shape[0], q.shape[1], self.num_heads, self.hidden_dim // self.num_heads)
        kh = k.view(k.shape[0], self.num_heads, self.hidden_dim // self.num_heads, k.shape[-2], k.shape[-1])
        weights = torch.einsum("bqnc,bnchw->bqnhw", qh * self.normalize_fact, kh)

        if mask is not None:
            weights.masked_fill_(mask.unsqueeze(1).unsqueeze(1), float("-inf"))
        weights = F.softmax(weights.flatten(2), dim=-1).view_as(weights)
        weights = self.dropout(weights)
        return weights


def dice_loss(inputs, targets, num_boxes):
    """
    Compute the DICE loss, similar to generalized IOU for masks
    Args:
        inputs: A float tensor of arbitrary shape.
                The predictions for each example.
        targets: A float tensor with the same shape as inputs. Stores the binary
                 classification label for each element in inputs
                (0 for the negative class and 1 for the positive class).
    """
    inputs = inputs.sigmoid()
    inputs = inputs.flatten(1)
    numerator = 2 * (inputs * targets).sum(1)
    denominator = inputs.sum(-1) + targets.sum(-1)
    loss = 1 - (numerator + 1) / (denominator + 1)
    return loss.sum() / num_boxes


def sigmoid_focal_loss(inputs, targets, num_boxes, alpha: float = 0.25, gamma: float = 2):
    """
    Loss used in RetinaNet for dense detection: https://arxiv.org/abs/1708.02002.
    Args:
        inputs: A float tensor of arbitrary shape.
                The predictions for each example.
        targets: A float tensor with the same shape as inputs. Stores the binary
                 classification label for each element in inputs
                (0 for the negative class and 1 for the positive class).
        alpha: (optional) Weighting factor in range (0,1) to balance
                positive vs negative examples. Default = -1 (no weighting).
        gamma: Exponent of the modulating factor (1 - p_t) to
               balance easy vs hard examples.
    Returns:
        Loss tensor
    """
    prob = inputs.sigmoid()
    ce_loss = F.binary_cross_entropy_with_logits(inputs, targets, reduction="none")
    p_t = prob * targets + (1 - prob) * (1 - targets)
    loss = ce_loss * ((1 - p_t) ** gamma)

    if alpha >= 0:
        alpha_t = alpha * targets + (1 - alpha) * (1 - targets)
        loss = alpha_t * loss

    return loss.mean(1).sum() / num_boxes


class PostProcessSegm(nn.Module):
    def __init__(self, threshold=0.5):
        super().__init__()
        self.threshold = threshold

    @torch.no_grad()
    def forward(self, results, outputs, orig_target_sizes, max_target_sizes):
        assert len(orig_target_sizes) == len(max_target_sizes)
        max_h, max_w = max_target_sizes.max(0)[0].tolist()
        outputs_masks = outputs["pred_masks"].squeeze(2)
        outputs_masks = F.interpolate(outputs_masks, size=(max_h, max_w), mode="bilinear", align_corners=False)
        outputs_masks = (outputs_masks.sigmoid() > self.threshold).cpu()

        for i, (cur_mask, t, tt) in enumerate(zip(outputs_masks, max_target_sizes, orig_target_sizes)):
            img_h, img_w = t[0], t[1]
            results[i]["masks"] = cur_mask[:, :img_h, :img_w].unsqueeze(1)
            results[i]["masks"] = F.interpolate(
                results[i]["masks"].float(), size=tuple(tt.tolist()), mode="nearest"
            ).byte()

        return results


