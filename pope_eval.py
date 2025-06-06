import argparse
import os
import random
import json
import numpy as np
import torch
import torch.backends.cudnn as cudnn
from tqdm import tqdm
from PIL import Image
from torchvision import transforms
from torchvision.transforms.functional import InterpolationMode
from torchvision.utils import save_image
from minigpt4.Halle_Editor.halle_editor import hall_editor
from pope_loader import POPEDataSet
from minigpt4.common.dist_utils import get_rank
from minigpt4.models import load_preprocess

from minigpt4.common.config import Config
from minigpt4.common.dist_utils import get_rank
from minigpt4.common.registry import registry

# imports modules for registration
from minigpt4.datasets.builders import *
from minigpt4.models import *
from minigpt4.processors import *
from minigpt4.runners import *
from minigpt4.tasks import *



MODEL_EVAL_CONFIG_PATH = {
    "minigpt4": "eval_configs/minigpt4_eval.yaml",
    "instructblip": "eval_configs/instructblip_eval.yaml",
    "lrv_instruct": "eval_configs/lrv_instruct_eval.yaml",
    "shikra": "eval_configs/shikra_eval.yaml",
    "llava-1.5": "eval_configs/llava-1.5_eval.yaml",
}

POPE_PATH = {
    "random": "pope_coco/coco_pope_random.json",
    "popular": "pope_coco/coco_pope_popular.json",
    "adversarial": "pope_coco/coco_pope_adversarial.json",
}

AOKVQA_PATH = {
    "random": "pope_coco/aokvqa_pope_random.json",
    "popular": "pope_coco/aokvqa_pope_popular.json",
    "adversarial": "pope_coco/aokvqa_pope_adversarial.json",
}

GQA_PATH = {
    "random": "pope_coco/gqa_pope_random.json",
    "popular": "pope_coco/gqa_pope_popular.json",
    "adversarial": "pope_coco/gqa_pope_adversarial.json",
}

INSTRUCTION_TEMPLATE = {
    "minigpt4": "###Human: <Img><ImageHere></Img> <question> ###Assistant:",
    "instructblip": "<ImageHere><question>",
    "lrv_instruct": "###Human: <Img><ImageHere></Img> <question> ###Assistant:",
    "shikra": "USER: <im_start><ImageHere><im_end> <question> ASSISTANT:",
    "llava-1.5": "USER: <ImageHere> <question> ASSISTANT:"
}


def parse_args():
    parser = argparse.ArgumentParser(description="POPE-Adv evaluation on LVLMs.")
    parser.add_argument("--model", type=str, help="model")
    parser.add_argument("--pope-type", type=str, help="model")
    # parser.add_argument("--cfg-path", required=True, help="path to configuration file.")
    parser.add_argument("--gpu-id", type=int, default=0, help="specify the gpu to load the model.")
    parser.add_argument(
        "--options",
        nargs="+",
        help="override some settings in the used config, the key-value pair "
        "in xxx=yyy format will be merged into config file (deprecate), "
        "change to --cfg-options instead.",
    )
    parser.add_argument("--data_path", type=str, default="COCO_2014/val2014/", help="data path")
    parser.add_argument("--batch_size", type=int, default=1, help="batch size")
    parser.add_argument("--num_workers", type=int, default=2, help="num workers")

    parser.add_argument("--beam", type=int)
    parser.add_argument("--sample", action='store_true')
    parser.add_argument("--scale_factor", type=float, default=50)
    parser.add_argument("--threshold", type=int, default=15)
    parser.add_argument("--num_attn_candidates", type=int, default=5)
    parser.add_argument("--penalty_weights", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    return args


def setup_seeds(seed):
    # seed = config.run_cfg.seed + get_rank()

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    cudnn.benchmark = False
    cudnn.deterministic = True


def print_acc(pred_list, label_list,args):
    pos = 1
    neg = 0
    yes_ratio = pred_list.count(1) / len(pred_list)
    # unknown_ratio = pred_list.count(2) / len(pred_list)

    TP, TN, FP, FN = 0, 0, 0, 0
    for pred, label in zip(pred_list, label_list):
        if pred == pos and label == pos:
            TP += 1
        elif pred == pos and label == neg:
            FP += 1
        elif pred == neg and label == neg:
            TN += 1
        elif pred == neg and label == pos:
            FN += 1
    img_save = {}
    print('TP\tFP\tTN\tFN\t')
    print('{}\t{}\t{}\t{}'.format(TP, FP, TN, FN))

    precision = float(TP) / float(TP + FP)
    recall = float(TP) / float(TP + FN)
    f1 = 2*precision*recall / (precision + recall)
    acc = (TP + TN) / (TP + TN + FP + FN)
    img_save["Accuracy"] = acc
    img_save["Precision"] = precision
    img_save["Recall"] = recall
    img_save["F1 score"] = f1
    img_save["Yes ratio"] = yes_ratio
    print('Accuracy: {}'.format(acc))
    print('Precision: {}'.format(precision))
    print('Recall: {}'.format(recall))
    print('F1 score: {}'.format(f1))
    print('Yes ratio: {}'.format(yes_ratio))
    with open(os.path.join("./log/gqa/", '{}_seed{}.jsonl'.format(args.pope_type,args.seed)), "a") as f:
        json.dump(img_save, f)
        f.write('\n')


def recorder(out, pred_list):
    NEG_WORDS = ["No", "not", "no", "NO"]
    for line in out:

        line = line.replace('.', '')
        line = line.replace(',', '')
        words = line.split(' ')
        if any(word in NEG_WORDS for word in words) or any(word.endswith("n't") for word in words):
            pred_list.append(0)
        else:
            pred_list.append(1)
    
    return pred_list




def main():

    args = parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu_id)

    args.cfg_path = MODEL_EVAL_CONFIG_PATH[args.model]
    args.pope_path = POPE_PATH[args.pope_type]
    cfg = Config(args)

    setup_seeds(args.seed)
    device = torch.device("cuda") if torch.cuda.is_available() else "cpu"

    # ========================================
    #             Model Initialization
    # ========================================
    print('Initializing Model')

    model_config = cfg.model_cfg
    model_config.device_8bit = args.gpu_id
    model_cls = registry.get_model_class(model_config.arch)
    model = model_cls.from_config(model_config).to(device)
    model.eval()
    vis_processors, txt_processors = load_preprocess(cfg.get_config().preprocess)
    # vis_processors.do_normalize = False
    # print(vis_processors["eval"].transform)
    print("Done!")
    target_dict={}
    query_dict={}
    halle_dict={}
    res_dict={}
    img_dict={}
    img_name_dict={}
    id_list=[]
    template = INSTRUCTION_TEMPLATE[args.model]
    for q in open(args.pope_path, 'r'):
        line = json.loads(q)
        id = line["question_id"]
        img_name_dict[id]=line["image"]
        target_dict[id]=line["label"]
        id_list.append(id)
        if line["label"] == "yes":
            halle_dict[id]= "no"
            res_dict[id] = 1
        else:
            halle_dict[id]= "yes"
            res_dict[id] = 0
        query_dict[id]=template.replace("<question>", line["text"])
    test_list = id_list[2000:]
    id_list = random.sample(id_list[:2000],1000)
    test_data = random.sample(test_list,500)
    for id in id_list:
        image_path = os.path.join(args.data_path, img_name_dict[id])
        if os.path.exists(image_path):
            raw_image = Image.open(image_path).convert("RGB")
        else:
            image_path = os.path.join("../halle/playground/data/coco/val2017", img_name_dict[id])
            raw_image = Image.open(image_path).convert("RGB")
        image = vis_processors["eval"](raw_image).unsqueeze(0)
        image = image.to(device)
        img_dict[id]=image
    # load pope data
    requests = {"id": id_list, "image": img_dict, "prompt": query_dict, "target": target_dict, "halle": halle_dict}
    base_dir  = "./edited_model/" + args.model
#unlearning
    hparams = 'minigpt4/Halle_Editor/llama-7b.yaml'
    halle_editor = hall_editor(hparams,model, requests,device,pope=True)
    edited_model = halle_editor.edit(args)
    save_path = f'{base_dir}/pope-1000-adversarial-chat'
    edited_model.save_pretrained(save_path)
    print(f"edited model is saved in {save_path}")
    exit()

    print ("load data finished")


    print("Start eval...")
    pred_list, pred_list_s, label_list = [], [], []
    for id in test_data:
        image = img_dict[id]
        qu = query_dict[id]
        label = res_dict[id]
        label_list.append(label)
        image = image.to(device)
        label = torch.Tensor(label).to(device)
        with torch.inference_mode():
            with torch.no_grad():
                _,out = model.generate(
                    {"image": image, "prompt":qu}, 
                    use_nucleus_sampling=args.sample, 
                    num_beams=args.beam,
                    max_new_tokens=10,
                    output_attentions=True,
                    opera_decoding=False,
                    scale_factor=args.scale_factor,
                    threshold=args.threshold,
                    num_attn_candidates=args.num_attn_candidates,
                    penalty_weights=args.penalty_weights,
                )
                pred_list = recorder(out, pred_list)
                # for line in out:
                #     print(line)
    print("[{}, {}]===============================================".format(args.scale_factor, args.num_attn_candidates))
    if len(pred_list) != 0:
        print_acc(pred_list, label_list,args)
    if len(pred_list_s) != 0:
        print_acc(pred_list_s, label_list,args)


if __name__ == "__main__":
    main()
