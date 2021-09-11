# PiT
# Copyright 2021-present NAVER Corp.
# Apache License v2.0

import torch
from einops import rearrange
from torch import nn
import math
import json
import torch.nn.functional as F

from functools import partial
from timm.models.layers import trunc_normal_
from timm.models.registry import register_model

file = 'score.json'

class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def softmax_with_policy(self, attn, policy, eps=1e-6):
        B, N, _ = policy.size()
        B, H, N, N = attn.size()
        attn_policy = policy.reshape(B, 1, 1, N)  # * policy.reshape(B, 1, N, 1)
        eye = torch.eye(N, dtype=attn_policy.dtype, device=attn_policy.device).view(1, 1, N, N)
        attn_policy = attn_policy + (1.0 - attn_policy) * eye
        max_att = torch.max(attn, dim=-1, keepdim=True)[0]
        attn = attn - max_att

        attn = attn.to(torch.float32).exp_() * attn_policy.to(torch.float32)
        attn = (attn + eps/N) / (attn.sum(dim=-1, keepdim=True) + eps)
        return attn.type_as(max_att)

    def forward(self, x, policy):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)

        attn = (q @ k.transpose(-2, -1)) * self.scale

        if policy is None:
            attn = attn.softmax(dim=-1)
        elif not self.training:
            attn = self.softmax_with_policy(attn, policy, 0)
        else:
            attn = self.softmax_with_policy(attn, policy, 1e-6)

        x = (attn @ v).transpose(1, 2).reshape(B, N, C)

        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

    def forward(self, x, policy=None):
        x = x + self.drop_path(self.attn(self.norm1(x), policy=policy))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x

class Transformer(nn.Module):
    def __init__(self, base_dim, depth, heads, mlp_ratio,
                 drop_rate=.0, attn_drop_rate=.0, drop_path_prob=None, pruning_loc=None, token_ratio=None):
        super(Transformer, self).__init__()
        self.layers = nn.ModuleList([])
        embed_dim = base_dim * heads

        if drop_path_prob is None:
            drop_path_prob = [0.0 for _ in range(depth)]

        self.blocks = nn.ModuleList([
            Block(
                dim=embed_dim,
                num_heads=heads,
                mlp_ratio=mlp_ratio,
                qkv_bias=True,
                qk_scale=None,
                drop=drop_rate,
                attn_drop=attn_drop_rate,
                drop_path=drop_path_prob[i],
                norm_layer=partial(nn.LayerNorm, eps=1e-6)
            )
            for i in range(depth)])

        predictor_list = [MultiheadPredictorLG(heads,embed_dim) for _ in range(len(pruning_loc))]

        self.score_predictor = nn.ModuleList(predictor_list)
        self.token_ratio = token_ratio

    def forward(self, x, cls_tokens, policy):
        h, w = x.shape[2:4]
        x = rearrange(x, 'b c h w -> b (h w) c') # 此时 (h w) 是 token_numbers 了
        B = x.shape[0]
        token_length = cls_tokens.shape[1]

        p_count = 0
        out_pred_prob = []
        sparse = []
        score_dict = {}
        init_n = x.shape[1]
        prev_decision = policy[:, token_length:]
        x = torch.cat((cls_tokens, x), dim=1)
        for i, blk in enumerate(self.blocks):
            if i in self.pruning_loc:
                spatial_x = x[:, token_length:]
                pred_score = self.score_predictor[p_count](spatial_x, prev_decision).reshape(B, -1, 2)
                hard_keep_decision = F.gumbel_softmax(pred_score, hard=True)[:, :, 0:1] * prev_decision
                if self.training:
                    out_pred_prob.append(hard_keep_decision.reshape(B, init_n))
                    cls_policy = torch.ones(B, token_length, 1, dtype=hard_keep_decision.dtype, device=hard_keep_decision.device)
                    policy = torch.cat([cls_policy, hard_keep_decision], dim=1)
                    x = blk(x, policy=policy)
                    prev_decision = hard_keep_decision
                else:
                    cls_policy = torch.ones(B, token_length, 1, dtype=hard_keep_decision.dtype, device=hard_keep_decision.device)
                    policy = torch.cat([cls_policy, hard_keep_decision], dim=1)
                    zeros, unzeros = test_irregular_sparsity(p_count, policy)
                    sparse.append([zeros, unzeros])
                    x = blk(x,policy=policy)
                    prev_decision = hard_keep_decision
                    score = pred_score[:, :, 0:1].cpu().numpy().tolist()
                    score_dict[p_count] = score[0] #144/12=12x30x87x4=125280= 1.5G
                p_count += 1
            else:
                x = blk(x, policy)

        cls_tokens = x[:, :token_length]
        x = x[:, token_length:]
        x = rearrange(x, 'b (h w) c -> b c h w', h=h, w=w)

        if self.training:
            if self.distill:
                return x, cls_tokens, policy, out_pred_prob # 注意的是传的是 policy 和 out_pred_prob, 需要更改loss.
            else:
                return x, cls_tokens, out_pred_prob
        else:
            with open(file, 'a') as f:
                json.dump(score_dict, f)
                f.write('\n')
            return x, cls_tokens


class MultiheadPredictorLG(nn.Module):
    """ Image to Patch Embedding
    """
    def __init__(self, num_heads=6, embed_dim=384):
        super().__init__()

        #print('head_num',num_heads)
        self.num_heads=num_heads
        self.embed_dim = embed_dim
        onehead_in_conv = nn.Sequential(
            nn.LayerNorm(embed_dim // num_heads),
            nn.Linear(embed_dim // num_heads, embed_dim // num_heads),
            nn.GELU()
        )

        onehead_out_conv = nn.Sequential(
            nn.Linear(embed_dim // num_heads, embed_dim // num_heads  // 2),
            nn.GELU(),
            nn.Linear(embed_dim // num_heads // 2, embed_dim // num_heads // 4),
            nn.GELU(),
            nn.Linear(embed_dim // num_heads // 4, 2),
            nn.LogSoftmax(dim=-1)
        )


        in_conv_list = [onehead_in_conv for _ in range(num_heads)]
        out_conv_list = [onehead_out_conv for _ in range(num_heads)]

        self.in_conv = nn.ModuleList(in_conv_list)
        self.out_conv = nn.ModuleList(out_conv_list)

    def forward(self, x, policy):

        multihead_score = 0
        for i in range(self.num_heads):
            x_single = x[:,:,self.embed_dim//self.num_heads*i:self.embed_dim//self.num_heads*(i+1)]   #([96, 196, 64])
            x_single = self.in_conv[i](x_single)
            B, N, C = x_single.size()       #([96, 196, 64])
            local_x = x_single[:,:, :C//2]  #([96, 196, 32])
            global_x = (x_single[:,:, C//2:] * policy).sum(dim=1, keepdim=True) / torch.sum(policy, dim=1, keepdim=True)  #([96, 1, 32])
            x_single = torch.cat([local_x, global_x.expand(B, N, C//2)], dim=-1)  #([96, 196, 64])
            score_single=self.out_conv[i](x_single) #([96, 196, 2])
            multihead_score += score_single
        multihead_score=multihead_score/self.num_heads
        return multihead_score


class conv_head_pooling(nn.Module):
    def __init__(self, in_feature, out_feature, stride,
                 padding_mode='zeros'):
        super(conv_head_pooling, self).__init__()

        self.conv = nn.Conv2d(in_feature, out_feature, kernel_size=stride + 1,
                              padding=stride // 2, stride=stride,
                              padding_mode=padding_mode, groups=in_feature)
        self.fc = nn.Linear(in_feature, out_feature)

    def forward(self, x, cls_token):

        x = self.conv(x)
        cls_token = self.fc(cls_token)

        return x, cls_token


class conv_embedding(nn.Module):
    def __init__(self, in_channels, out_channels, patch_size,
                 stride, padding):
        super(conv_embedding, self).__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=patch_size,
                              stride=stride, padding=padding, bias=True)

    def forward(self, x):
        x = self.conv(x)
        return x


class PoolingTransformer(nn.Module):
    def __init__(self, image_size, patch_size, stride, base_dims, depth, heads,
                 mlp_ratio, num_classes=1000, in_chans=3,
                 attn_drop_rate=.0, drop_rate=.0, drop_path_rate=.0,
                 pruning_loc=None, token_ratio=None, distill=False):
        super(PoolingTransformer, self).__init__()

        total_block = sum(depth)
        padding = 0
        block_idx = 0

        width = math.floor(
            (image_size + 2 * padding - patch_size) / stride + 1)

        self.base_dims = base_dims
        self.heads = heads
        self.num_classes = num_classes

        self.patch_size = patch_size
        self.pos_embed = nn.Parameter(
            torch.randn(1, base_dims[0] * heads[0], width, width),
            requires_grad=True
        )
        self.patch_embed = conv_embedding(in_chans, base_dims[0] * heads[0],
                                          patch_size, stride, padding)

        self.cls_token = nn.Parameter(
            torch.randn(1, 1, base_dims[0] * heads[0]),
            requires_grad=True
        )
        self.pos_drop = nn.Dropout(p=drop_rate)

        self.transformers = nn.ModuleList([])
        self.pools = nn.ModuleList([])

        self.distill = distill

        self.pruning_loc = pruning_loc  # 不同阶段就插一个吧。我不求了。。。
        self.token_ratio = token_ratio

        for stage in range(len(depth)):
            drop_path_prob = [drop_path_rate * i / total_block
                              for i in range(block_idx, block_idx + depth[stage])]
            block_idx += depth[stage]

            self.transformers.append(
                Transformer(base_dims[stage], depth[stage], heads[stage], # 不同的 stage，三种不同模式的 transformer
                            mlp_ratio,
                            drop_rate, attn_drop_rate, drop_path_prob, pruning_loc[stage], token_ratio[stage])
            )
            if stage < len(heads) - 1:
                self.pools.append(
                    conv_head_pooling(base_dims[stage] * heads[stage],
                                      base_dims[stage + 1] * heads[stage + 1],
                                      stride=2
                                      )
                )

        self.norm = nn.LayerNorm(base_dims[-1] * heads[-1], eps=1e-6)
        self.embed_dim = base_dims[-1] * heads[-1]

        # Classifier head
        if num_classes > 0:
            self.head = nn.Linear(base_dims[-1] * heads[-1], num_classes)
        else:
            self.head = nn.Identity()

        trunc_normal_(self.pos_embed, std=.02)
        trunc_normal_(self.cls_token, std=.02)
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token'}

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        if num_classes > 0:
            self.head = nn.Linear(self.embed_dim, num_classes)
        else:
            self.head = nn.Identity()

    def forward_features(self, x):
        B = x.shape[0]
        x = self.patch_embed(x)

        pos_embed = self.pos_embed
        x = self.pos_drop(x + pos_embed)
        cls_tokens = self.cls_token.expand(x.shape[0], -1, -1)
        token_length = cls_tokens.shape[1]

        out_pred_prob = []
        for stage in range(len(self.pools)):
            h, w = x.shape[2:4]
            init_n = h * w
            policy = torch.ones(B, init_n + token_length, 1, dtype=x.dtype, device=x.device)
            if self.training:
                if self.distill:
                    x, cls_tokens, policy, sub_pred_prob= self.transformers[stage](x, cls_tokens, policy = policy)
                else:
                    x, cls_tokens, sub_pred_prob= self.transformers[stage](x, cls_tokens, policy = policy)
                out_pred_prob = out_pred_prob + sub_pred_prob
            else:
                x, cls_tokens = self.transformers[stage](x, cls_tokens, policy = policy)
            x, cls_tokens = self.pools[stage](x, cls_tokens)

        h, w = x.shape[2:4]
        init_n = h * w
        policy = torch.ones(B, init_n + token_length, 1, dtype=x.dtype, device=x.device)
        if self.training:
            if self.distill:
                x, cls_tokens, policy, sub_pred_prob= self.transformers[-1](x, cls_tokens, policy = policy)
            else:
                x, cls_tokens, sub_pred_prob= self.transformers[-1](x, cls_tokens, policy = policy)
            out_pred_prob = out_pred_prob + sub_pred_prob
        else:
            x, cls_tokens = self.transformers[-1](x, cls_tokens, policy = policy)

        cls_tokens = self.norm(cls_tokens)

        if self.training: # x ==  features
            return cls_tokens, x, policy[:, token_length:], out_pred_prob
        else:
            return cls_tokens, x, policy[:, token_length:], None

    def forward(self, x):
        cls_token, features, prev_decision, out_pred_prob = self.forward_features(x)
        cls_token = self.head(cls_token[:, 0])
        if self.training:
            if self.distill:
                return cls_token, features, prev_decision.detach(), out_pred_prob
            else:
                return cls_token, out_pred_prob
        else:
            return cls_token




class PoolingTransformerTeacher(nn.Module):
    def __init__(self, image_size, patch_size, stride, base_dims, depth, heads,
                 mlp_ratio, num_classes=1000, in_chans=3,
                 attn_drop_rate=.0, drop_rate=.0, drop_path_rate=.0):
        super(PoolingTransformerTeacher, self).__init__()

        total_block = sum(depth)
        padding = 0
        block_idx = 0

        width = math.floor(
            (image_size + 2 * padding - patch_size) / stride + 1)

        self.base_dims = base_dims
        self.heads = heads
        self.num_classes = num_classes

        self.patch_size = patch_size
        self.pos_embed = nn.Parameter(
            torch.randn(1, base_dims[0] * heads[0], width, width),
            requires_grad=True
        )
        self.patch_embed = conv_embedding(in_chans, base_dims[0] * heads[0],
                                          patch_size, stride, padding)

        self.cls_token = nn.Parameter(
            torch.randn(1, 1, base_dims[0] * heads[0]),
            requires_grad=True
        )
        self.pos_drop = nn.Dropout(p=drop_rate)

        self.transformers = nn.ModuleList([])
        self.pools = nn.ModuleList([])

        for stage in range(len(depth)):
            drop_path_prob = [drop_path_rate * i / total_block
                              for i in range(block_idx, block_idx + depth[stage])]
            block_idx += depth[stage]

            self.transformers.append(
                Transformer(base_dims[stage], depth[stage], heads[stage], # 不同的 stage，三种不同模式的 transformer
                            mlp_ratio,
                            drop_rate, attn_drop_rate, drop_path_prob)
            )
            if stage < len(heads) - 1:
                self.pools.append(
                    conv_head_pooling(base_dims[stage] * heads[stage],
                                      base_dims[stage + 1] * heads[stage + 1],
                                      stride=2
                                      )
                )

        self.norm = nn.LayerNorm(base_dims[-1] * heads[-1], eps=1e-6)
        self.embed_dim = base_dims[-1] * heads[-1]

        # Classifier head
        if num_classes > 0:
            self.head = nn.Linear(base_dims[-1] * heads[-1], num_classes)
        else:
            self.head = nn.Identity()

        trunc_normal_(self.pos_embed, std=.02)
        trunc_normal_(self.cls_token, std=.02)
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token'}

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        if num_classes > 0:
            self.head = nn.Linear(self.embed_dim, num_classes)
        else:
            self.head = nn.Identity()

    def forward_features(self, x):
        x = self.patch_embed(x)

        pos_embed = self.pos_embed
        x = self.pos_drop(x + pos_embed)
        cls_tokens = self.cls_token.expand(x.shape[0], -1, -1)

        for stage in range(len(self.pools)):
            x, cls_tokens = self.transformers[stage](x, cls_tokens)
            x, cls_tokens = self.pools[stage](x, cls_tokens)
        x, cls_tokens = self.transformers[-1](x, cls_tokens)

        cls_tokens = self.norm(cls_tokens)

        return cls_tokens, x

    def forward(self, x):
        cls_token, tokens = self.forward_features(x)
        cls_token = self.head(cls_token[:, 0])
        return cls_token, tokens

@register_model
def pit_b(pretrained, **kwargs):
    model = PoolingTransformer(
        image_size=224,
        patch_size=14,
        stride=7,
        base_dims=[64, 64, 64],
        depth=[3, 6, 4],
        heads=[4, 8, 16],
        mlp_ratio=4,
        **kwargs
    )
    if pretrained:
        state_dict = \
        torch.load('weights/pit_b_820.pth', map_location='cpu')
        model.load_state_dict(state_dict)
    return model

@register_model
def pit_s(pretrained, **kwargs):
    model = PoolingTransformer(
        image_size=224,
        patch_size=16,
        stride=8,
        base_dims=[48, 48, 48],
        depth=[2, 6, 4],
        heads=[3, 6, 12],
        mlp_ratio=4,
        **kwargs
    )
    if pretrained:
        state_dict = \
        torch.load('weights/pit_s_809.pth', map_location='cpu')
        model.load_state_dict(state_dict)
    return model


@register_model
def pit_xs(pretrained, **kwargs):
    model = PoolingTransformer(
        image_size=224,
        patch_size=16,
        stride=8,
        base_dims=[48, 48, 48],
        depth=[2, 6, 4],
        heads=[2, 4, 8],
        mlp_ratio=4,
        **kwargs
    )
    if pretrained:
        state_dict = \
        torch.load('weights/pit_xs_781.pth', map_location='cpu')
        model.load_state_dict(state_dict)
    return model

@register_model
def pit_ti(pretrained, **kwargs):
    model = PoolingTransformer(
        image_size=224,
        patch_size=16,
        stride=8,
        base_dims=[32, 32, 32],
        depth=[2, 6, 4],
        heads=[2, 4, 8],
        mlp_ratio=4,
        **kwargs
    )
    if pretrained:
        state_dict = \
        torch.load('weights/pit_ti_730.pth', map_location='cpu')
        model.load_state_dict(state_dict)
    return model


def test_irregular_sparsity(name,matrix):

    # continue
    zeros = np.sum(matrix.cpu().detach().numpy() == 0)

    non_zeros = np.sum(matrix.cpu().detach().numpy() != 0)

    # print(name, non_zeros)
    print(" {}, all weights: {}, irregular zeros: {}, irregular sparsity is: {:.4f}".format( name, zeros+non_zeros, zeros, zeros / (zeros + non_zeros)))
    # print(non_zeros+zeros)
    # total_nonzeros += 128000

    return zeros,non_zeros
