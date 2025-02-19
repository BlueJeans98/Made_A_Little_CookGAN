import os
import string
import json
import numpy as np
import re
import copy
from datetime import datetime
import json
import argparse
from torch.nn import functional as F
import torch
from torch import device
from PIL import Image
import numpy as np
from torchvision import utils as vutils
from matplotlib import pyplot as plt
import sys

import models_retrieval_nobak
import models_cookgan_for_retrieval

root = '/data/CS470_HnC'

def clean_state_dict(state_dict):
    # create new OrderedDict that does not contain `module.`
    from collections import OrderedDict
    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        name = k[7:] if k[:min(6,len(k))] == 'module' else k # remove `module.`
        new_state_dict[name] = v
    return new_state_dict

def sample_data(loader):
    """
    arguments:
        loader: torch.utils.data.DataLoader
    return:
        one batch of data
    usage:
        data = next(sample_data(loader))
    """
    while True:
        for batch in loader:
            yield batch

def str2bool(v):
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def dspath(ext, ROOT, **kwargs):
    return os.path.join(ROOT,ext)

class Layer(object):
    L1 = 'layer1'
    L2 = 'layer2'
    L3 = 'layer3'
    INGRS = 'det_ingrs'

    @staticmethod
    def load(name, ROOT, **kwargs):
        with open(dspath(name + '.json',ROOT, **kwargs)) as f_layer:
            return json.load(f_layer)

    @staticmethod
    def merge(layers, ROOT,copy_base=False, **kwargs):
        layers = [l if isinstance(l, list) else Layer.load(l, ROOT, **kwargs) for l in layers]
        base = copy.deepcopy(layers[0]) if copy_base else layers[0]
        entries_by_id = {entry['id']: entry for entry in base}
        for layer in layers[1:]:
            for entry in layer:
                base_entry = entries_by_id.get(entry['id'])
                if not base_entry:
                    continue
                base_entry.update(entry)
        return base

def remove_numbers(s):
    '''
    remove numbers in a sentence.
    - 1.1:  \d+\.\d+
    - 1 1/2 or 1-1/2 or 1 -1/2 or 1- 1/2 or 1 - 1/2: (\d+ *-* *)?\d+/\d+
    - 1: \d+'
    
    Arguments:
        s {str} -- the string to operate on
    
    Returns:
        str -- the modified string without numbers
    '''
    return re.sub(r'\d+\.\d+|(\d+ *-* *)?\d+/\d+|\d+', 'some', s)

def tok(text, ts=False):
    if not ts:
        ts = [',','.',';','(',')','?','!','&','%',':','*','"']
    for t in ts:
        text = text.replace(t,' ' + t + ' ')
    return text


param_counter = lambda params: sum(p.numel() for p in params if p.requires_grad)


def load_recipes(file_path, part=None):
    with open(file_path, 'r') as f:
        info = json.load(f)
    if part:
        info = [x for x in info if x['partition']==part]
    return info


def get_title_wordvec(recipe, w2i, max_len=20):
    '''
    get the title wordvec for the recipe, the 
    number of items might be different for different 
    recipe
    '''
    title = recipe['title']
    words = title.split()
    vec = np.zeros([max_len], dtype=np.int)
    num_words = min(max_len, len(words))
    for i in range(num_words):
        word = words[i]
        if word not in w2i:
            word = '<other>'
        vec[i] = w2i[word]
    return vec, num_words


def get_instructions_wordvec(recipe, w2i, max_len=20):
    '''
    get the instructions wordvec for the recipe, the 
    number of items might be different for different 
    recipe
    '''
    instructions = recipe['instructions']
    # each recipe has at most max_len sentences
    # each sentence has at most max_len words
    vec = np.zeros([max_len, max_len], dtype=np.int)
    num_insts = min(max_len, len(instructions))
    num_words_each_inst = np.zeros(max_len, dtype=np.int)
    for row in range(num_insts):
        inst = instructions[row]
        words = inst.split()
        num_words = min(max_len, len(words))
        num_words_each_inst[row] = num_words
        for col in range(num_words):
            word = words[col]
            if word not in w2i:
                word = '<other>'
            vec[row, col] = w2i[word]
    return vec, num_insts, num_words_each_inst


def get_ingredients_wordvec(recipe, w2i, permute_ingrs=False, max_len=20):
    '''
    get the ingredients wordvec for the recipe, the 
    number of items might be different for different 
    recipe
    '''
    ingredients = recipe['ingredients']
    if permute_ingrs:
        ingredients = np.random.permutation(ingredients).tolist()
    vec = np.zeros([max_len], dtype=np.int)
    num_words = min(max_len, len(ingredients))
        
    for i in range(num_words):
        word = ingredients[i]
        if word not in w2i:
            word = '<other>'
        vec[i] = w2i[word]
        
    return vec, num_words


def get_ingredients_wordvec_withClasses(recipe, w2i, ingr2i, permute_ingrs=False, max_len=20):
    '''
    get the ingredients wordvec for the recipe, the 
    number of items might be different for different 
    recipe
    '''
    ingredients = recipe['ingredients']
    if permute_ingrs:
        ingredients = np.random.permutation(ingredients).tolist()

    label = np.zeros([len(ingr2i)], dtype=np.float32)

    vec = np.zeros([max_len], dtype=np.int)
    num_words = min(max_len, len(ingredients))
        
    for i in range(num_words):
        word = ingredients[i]
        if word not in w2i:
            word = '<other>'
        vec[i] = w2i[word]

        if word in ingr2i:
            label[ingr2i[word]] = 1
        
    return vec, num_words, label

def requires_grad(model, flag=True):
    for p in model.parameters():
        p.requires_grad = flag

def load_dict(file_path):
    with open(file_path, 'r') as f_vocab:
        w2i = {w.rstrip(): i+3 for i, w in enumerate(f_vocab)}
        w2i['<end>'] = 1
        w2i['<other>'] = 2
    return w2i

def load_model(ckpt_path, device='cuda'):
    #print('load retrieval model from:', ckpt_path)
    ckpt = torch.load(ckpt_path)
    ckpt_args = ckpt['args']
    batch_idx = ckpt['batch_idx']
    text_encoder, image_encoder, optimizer = create_model(ckpt_args, device)
    if device=='cpu':
        text_encoder.load_state_dict(ckpt['text_encoder'])
        image_encoder.load_state_dict(ckpt['image_encoder'])
    else:
        text_encoder.module.load_state_dict(ckpt['text_encoder'])
        image_encoder.module.load_state_dict(ckpt['image_encoder'])
    optimizer.load_state_dict(ckpt['optimizer'])

    return text_encoder, image_encoder

def compute_txt_feature(recipes, TxtEnc, word2i, ingr2i):
    i = 0
    pass_count = 0
    txt_feat = torch.empty(1,1024)
    for recipe in recipes:
        #print(i)
        if len(recipe['ingredients']) >= 20:
            pass_count+=1
            continue
        title_vec, ingrs_vec, insts_vec = vectorize(recipe, word2i, ingr2i)
        title_vec = title_vec.repeat(1, 1)
        ingrs_vec = ingrs_vec.repeat(1, 1)
        insts_vec = insts_vec.repeat(1, 1, 1)
        # text_feature = TxtEnc([title_vec, ingrs_vec, insts_vec])
        # print(text_feature)
        cur_txt_feat = TxtEnc([title_vec, ingrs_vec, insts_vec])
        if i == 0:
            txt_feat = cur_txt_feat
        else:
            txt_feat = torch.cat((txt_feat, cur_txt_feat),0)
        i+=1
    return txt_feat

def vectorize(recipe, word2i, ingr2i):
    """data preprocessing, from recipe text to one-hot inputs

    Arguments:
        recipe {dict} -- a dictionary with 'title', 'ingredients', 'instructions'
        word2i {dict} -- word mapping for title and instructions
        ingr2i {dict} -- ingredient mapping

    Returns:
        list -- a list of three tensors [title, ingredients and instructions]
    """    
    title, _ = get_title_wordvec(recipe, word2i) # np.int [max_len]
    ingredients, _ = get_ingredients_wordvec(recipe, ingr2i, permute_ingrs=False) # np.int [max_len]
    instructions, _, _ = get_instructions_wordvec(recipe, word2i) # np.int [max_len, max_len]
    return [torch.tensor(x).unsqueeze(0) for x in [title, ingredients, instructions]]

def generate_images(ingredients, batch) :

    word2i = load_dict('/data/CS470_HnC/made_a_little_cookgan/vocab_inst.txt')
    ingr2i = load_dict('/data/CS470_HnC/made_a_little_cookgan/vocab_ingr.txt')

    text_encoder = models_retrieval_nobak.TextEncoder(
    data_dir='/data/CS470_HnC/made_a_little_cookgan/', text_info='010', hid_dim=300,
    emb_dim=300, z_dim=1024, with_attention=2,
    ingr_enc_type='rnn').eval()
    text_encoder.load_state_dict(torch.load('/data/CS470_HnC/made_a_little_cookgan/text_encoder.model'))

    netG = models_cookgan_for_retrieval.G_NET(levels=3).eval().requires_grad_(False)
    netG.load_state_dict(torch.load('/data/CS470_HnC/made_a_little_cookgan/gen_salad_cycleTxt1.0_e300.model'))


    title = 'dummy title'
    instructions = 'dummy instructions'

    recipe = {
        'title': title,
        'ingredients': [x.replace(' ', '_') for x in ingredients],
        'instructions': instructions
    }
    title_vec, ingrs_vec, insts_vec = vectorize(recipe, word2i, ingr2i)
    title_vec = title_vec.repeat(batch, 1)
    ingrs_vec = ingrs_vec.repeat(batch, 1)
    insts_vec = insts_vec.repeat(batch, 1, 1)
    noise = torch.FloatTensor(batch, 100).normal_(0, 1)
    text_feature = text_encoder([title_vec, ingrs_vec, insts_vec])
    
    imgs, _, _ = netG(noise, text_feature)

    return imgs

def compute_img_feature(uniques, img_encoder):
    feat = torch.empty(1,1024)
    for i in range (len(uniques)):
        imgs = generate_images(uniques[i], 1)
        img = imgs[2]

        mean = [0.485, 0.456, 0.406]
        std = [0.229, 0.224, 0.225]
        img = img/2 + 0.5
        img = F.interpolate(img, [224, 224], mode='bilinear', align_corners=True)
        for i in range(img.shape[1]):
            img[:,i] = (img[:,i]-mean[i])/std[i]
        cur_feat = img_encoder(img)

        if i == 0:
            feat = cur_feat
        else:
            feat = torch.cat((feat, cur_feat),0)

    return img, feat

def compute_ingredient_retrival_score(imgs, txts, tops):
    imgs = imgs / np.linalg.norm(imgs, axis=1)[:, None]
    txts = txts / np.linalg.norm(txts, axis=1)[:, None]
    # retrieve recipe
    sims = np.dot(imgs, txts.T) # [N, N]
    # loop through the N similarities for images
    cvgs = []
    for ii in range(imgs.shape[0]):
        # get a row of similarities for image ii
        sim = sims[ii,:]
        # sort indices in descending order
        sorting = np.argsort(sim)[::-1].tolist()
        topk_idxs = sorting[:tops]
        #print(topk_idxs)
        success = 0.0
        for rcp_idx in topk_idxs:
            rcp = recipes[rcp_idx]
            ingrs = rcp['new_ingrs']
            if hot_ingr in ingrs:
                success += 1
        cvgs.append(success / tops)
    return np.array(cvgs)