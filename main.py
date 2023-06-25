# -*- coding: utf-8 -*-
"""Demo_ConZIC.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1BiNlzNV3LkQY2W9qICOnlBSxwkxLnA-W
"""

import nltk
import os
import sys
import time
import argparse
from PIL import Image
import json
import torch
from transformers import AutoModelForMaskedLM, AutoTokenizer
import functools
from flask import Flask, render_template, request, flash, redirect, session
from werkzeug.utils import secure_filename

from ConZIC import generate_caption, control_generate_caption, create_logger, set_seed
from ConZIC.clip.clip import CLIP

# Commented out IPython magic to ensure Python compatibility.
# @title Prepare the running enviroment

img_name = ''
upload_img_path = ''
is_gpu = False  # @param {type:"boolean"}

app = Flask(__name__)


def get_args():
    parser = argparse.ArgumentParser()

    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--batch_size", type=int, default=1, help="Only supports batch_size=1 currently.")
    parser.add_argument("--device", type=str,
                        default='cuda', choices=['cuda', 'cpu'])

    # Generation and Controllable Type
    parser.add_argument('--run_type',
                        default='caption',
                        nargs='?',
                        choices=['caption', 'controllable'])
    parser.add_argument('--prompt',
                        default='Image of a', type=str)
    parser.add_argument('--order',
                        default='shuffle',
                        nargs='?',
                        choices=['sequential', 'shuffle', 'span', 'random', 'parallel'],
                        help="Generation order of text")
    parser.add_argument('--control_type',
                        default='sentiment',
                        nargs='?',
                        choices=["sentiment", "pos"],
                        help="which controllable task to conduct")
    parser.add_argument('--pos_type', type=list,
                        default=[['DET'], ['ADJ', 'NOUN'], ['NOUN'],
                                 ['VERB'], ['VERB'], ['ADV'], ['ADP'],
                                 ['DET', 'NOUN'], ['NOUN'], ['NOUN', '.'],
                                 ['.', 'NOUN'], ['.', 'NOUN']],
                        help="predefined part-of-speech templete")
    parser.add_argument('--sentiment_type',
                        default="positive",
                        nargs='?',
                        choices=["positive", "negative"])
    parser.add_argument('--samples_num',
                        default=2, type=int)  # Nghĩa là số  sample(caption) được gen ra từ mô hình.

    # Hyperparameters
    parser.add_argument("--sentence_len", type=int, default=10)
    parser.add_argument("--candidate_k", type=int, default=200)
    parser.add_argument("--alpha", type=float, default=0.02, help="weight for fluency")
    parser.add_argument("--beta", type=float, default=2.0, help="weight for image-matching degree")
    parser.add_argument("--gamma", type=float, default=5.0, help="weight for controllable degree")
    parser.add_argument("--lm_temperature", type=float, default=0.1)
    parser.add_argument("--num_iterations", type=int, default=10, help="predefined iterations for Gibbs Sampling")

    # Models and Paths
    parser.add_argument("--lm_model", type=str, default='bert-base-uncased',
                        help="Path to language model")  # bert,roberta
    parser.add_argument("--match_model", type=str, default='openai/clip-vit-base-patch32',
                        help="Path to Image-Text model")  # clip,align
    parser.add_argument("--caption_img_path", type=str, default='./examples/girl.jpg',
                        help="file path of the image for captioning")
    parser.add_argument("--stop_words_path", type=str, default='ConZIC/stop_words.txt',
                        help="Path to stop_words.txt")
    parser.add_argument("--add_extra_stopwords", type=list, default=[],
                        help="you can add some extra stop words")

    args = parser.parse_args(args=[])
    return args


args = get_args()


@app.route("/")
def index():
    nltk.download('wordnet')
    nltk.download('punkt')
    nltk.download('averaged_perceptron_tagger')
    nltk.download('sentiwordnet')

    # @title Upload your image or use our examples
    upload_your_image = False  # @param {type:"boolean"}

    # @title Select examples
    # example_name = 'cat.png'  # @param ['cat.png', 'girl.jpg', 'Gosh.jpeg', 'horse.png']
    # example_img_path = os.path.join(os.getcwd(), "ConZIC")
    # example_img_path = os.path.join(example_img_path, example_name)
    # %cd /content/ConZIC/your_uploaded_image

    return render_template("home.html")


@app.route('/uploader', methods=['GET', 'POST'])
def upload_file():

    if request.method == 'POST':
        f = request.files['fileBtn']
        if f.filename == '':
            return render_template("home.html", message="Please choose a picture")

        app.config['UPLOAD_FOLDER'] = os.path.join('static', 'pictures')
        # Pictures is in folder /static/pictures
        if not os.path.isdir(app.config['UPLOAD_FOLDER']):
            os.makedirs(os.path.abspath(app.config['UPLOAD_FOLDER']))
        f.save(os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(f.filename)))

        global img_name
        img_name = f.filename
        global upload_img_path
        upload_img_path = os.path.join(app.config['UPLOAD_FOLDER'], img_name)

        return render_template("Uploaded.html")


@app.route('/configure', methods=['GET', 'POST'])
def configure():
    if request.method == 'POST':
        global is_gpu
        is_gpu = int(request.form.get('isGpu'))
        args.sentence_len = int(request.form.get('length'))
        args.run_type = request.form.get('runType')
        args.control_type = request.form.get('controlType')
        args.sentiment_type = request.form.get('sentimentType')
        args.alpha = float(request.form.get('alpha'))
        args.beta = float(request.form.get('beta'))
        args.gamma = float(request.form.get('gamma'))
        args.samples_num = int(request.form.get('samplesNum'))
        args.order = request.form.get('order')
        args.num_iterations = int(request.form.get('numIterations'))
        args.caption_img_path = upload_img_path
        args.device = "cuda" if is_gpu else "cpu"
        set_seed(args.seed)
        return render_template("processing.html")


@app.route('/processing')
def processing():
    run_type = "caption" if args.run_type == "caption" else args.control_type
    if run_type == "sentiment":
        run_type = args.sentiment_type

    if not os.path.exists("ConZIC/logger"):
        os.mkdir("ConZIC/logger")
    logger = create_logger(
        "ConZIC/logger", 'demo_{}_{}_len{}_topk{}_alpha{}_beta{}_gamma{}_lmtemp{}_{}.log'.format(
            run_type, args.order, args.sentence_len,
            args.candidate_k, args.alpha, args.beta, args.gamma, args.lm_temperature,
            time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime())))

    logger.info(f"Generating order:{args.order}")
    logger.info(f"Run type:{run_type}")
    logger.info(args)

    # @title Load pre-trained model (weights)
    lm_model = AutoModelForMaskedLM.from_pretrained(args.lm_model)
    lm_tokenizer = AutoTokenizer.from_pretrained(args.lm_model)
    lm_model.eval()
    clip = CLIP(args.match_model)
    clip.eval()

    lm_model = lm_model.to(args.device)
    clip = clip.to(args.device)

    # @title Remove stop words, token mask
    with open(args.stop_words_path, 'r', encoding='utf-8') as stop_words_file:
        stop_words = stop_words_file.readlines()
        stop_words_ = [stop_word.rstrip('\n') for stop_word in stop_words]
        stop_words_ += args.add_extra_stopwords
        stop_ids = lm_tokenizer.convert_tokens_to_ids(stop_words_)
        token_mask = torch.ones((1, lm_tokenizer.vocab_size))
        for stop_id in stop_ids:
            token_mask[0, stop_id] = 0
        token_mask = token_mask.to(args.device)

    # title Run
    img_path = upload_img_path  # if upload_your_image else example_img_path
    if args.run_type == 'caption':
        FinalCaption, BestCaption = run_caption(args, img_path, lm_model, lm_tokenizer, clip, token_mask, logger)
    elif args.run_type == 'controllable':
        FinalCaption, BestCaption = run_control(run_type, args, img_path, lm_model, lm_tokenizer, clip, token_mask,
                                                logger)
    else:
        raise Exception('run_type must be caption or controllable!')

    # @title Output
    # Image.open(img_path).show()
    print("Final Caption\n")
    for i in range(len(FinalCaption)):
        print(f"{FinalCaption[i]}\n")
    print("Best Caption\n")
    for i in range(len(BestCaption)):
        print(f"{BestCaption[i]}\n")

    return render_template("Result.html", fc=FinalCaption[0], bc=BestCaption[0], image=img_path)


# if not uploaded:
#    img_name = ''
# elif len(uploaded) == 1:
#    img_name = list(uploaded.keys())[0]
# else:
#    raise AssertionError('Please upload one image at a time')

# upload_img_path = os.path.join(os.getcwd(), img_name)
# print(upload_img_path)
# %cd /content/ConZIC

# @title Define parameters


# @title Image captioning

def run_caption(args, image_path, lm_model, lm_tokenizer, clip, token_mask, logger):
    FinalCaptionList = []
    BestCaptionList = []
    logger.info(f"Processing: {image_path}")
    image_instance = Image.open(image_path).convert("RGB")
    # img_name = [image_path.spilt("/")[-1]]
    for sample_id in range(args.samples_num):
        logger.info(f"Sample {sample_id}: ")
        gen_texts, clip_scores = generate_caption(img_name, lm_model, clip, lm_tokenizer, image_instance, token_mask,
                                                  logger,
                                                  prompt=args.prompt, batch_size=args.batch_size,
                                                  max_len=args.sentence_len,
                                                  top_k=args.candidate_k, temperature=args.lm_temperature,
                                                  max_iter=args.num_iterations, alpha=args.alpha, beta=args.beta,
                                                  generate_order=args.order)
        str1 = ''
        for i in gen_texts[-2]:
            str1 += i
        str2 = ''
        for i in gen_texts[-1]:
            str2 += i

        FinalCaptionStr = "Sample {}: ".format(sample_id + 1) + str1
        BestCaptionStr = "Sample {}: ".format(sample_id + 1) + str2
        FinalCaptionList.append(FinalCaptionStr)
        BestCaptionList.append(BestCaptionStr)
    return FinalCaptionList, BestCaptionList


def run_control(run_type, args, image_path, lm_model, lm_tokenizer, clip, token_mask, logger):
    FinalCaptionList = []
    BestCaptionList = []
    logger.info(f"Processing: {image_path}")
    image_instance = Image.open(image_path).convert("RGB")
    for sample_id in range(args.samples_num):
        logger.info(f"Sample {sample_id}: ")
        gen_texts, clip_scores = control_generate_caption(img_name, lm_model, clip, lm_tokenizer, image_instance,
                                                          token_mask, logger,
                                                          prompt=args.prompt, batch_size=args.batch_size,
                                                          max_len=args.sentence_len,
                                                          top_k=args.candidate_k, temperature=args.lm_temperature,
                                                          max_iter=args.num_iterations, alpha=args.alpha,
                                                          beta=args.beta, gamma=args.gamma,
                                                          ctl_type=args.control_type, style_type=args.sentiment_type,
                                                          pos_type=args.pos_type, generate_order=args.order)
        print("Test for debugging: ", len(gen_texts))
        gen_texts = functools.reduce(lambda x, y: x + y, gen_texts)
        # Chuyển gentexts từ dạng hierachy list thành dạng list chỉ 1 string, không bao gồm các list con.
        str1 = ''
        for i in gen_texts[-2]:
            str1 += i
        str2 = ''
        for i in gen_texts[-1]:
            str2 += i

        FinalCaptionStr = "Sample {}: ".format(sample_id + 1) + str1

        BestCaptionStr = "Sample {}: ".format(sample_id + 1) + str2
        FinalCaptionList.append(FinalCaptionStr)
        BestCaptionList.append(BestCaptionStr)
    return FinalCaptionList, BestCaptionList


'''
*   RunType: Select RunType equal to "contrallable" to control text generation.
*   ControlType: Control text by sentiment or part of speech.
*   SentimentType: Control sentiment: positive or negative
*   Order: Generation order of text
*   Alpha: Weight for fluency; Choose between 0 and 1
*   Beta: Weight for image-matching degree; Choose between 1 and 5
*   Gamma: Weight for controllable degree; Choose between 1 and 10
*   SampleNum: Number of runs; Choose between 1 and 5
*   Length: Sentence length; Choose between 5 and 10
*   NumIterations: Iterations for Gibbs Sampling; Choose between 1 and 15
'''
'''
# @title Select types and parameters


RunType = 'controllable'  # @param ['caption', 'controllable']
ControlType = 'sentiment'  # @param ["sentiment","pos"]
SentimentType = 'positive'  # @param ["positive", "negative"]
Order = 'shuffle'  # @param ['sequential', 'shuffle', 'random']
Alpha = 0.08  # @param {type:"slider", min:0, max:1, step:0.01}
Beta = 2  # @param {type:"slider", min:1, max:5, step:0.5}
Gamma = 5  # @param {type:"slider", min:1, max:10, step:0.5}
SamplesNum = 2  # @param {type:"slider", min:1, max:5, step:1}
Length = 15  # @param {type:"slider", min:5, max:15,  step:1}
NumIterations = 5  # @param {type:"slider", min:1, max:15, step:1}


# @title Creat logger
'''
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=True)
