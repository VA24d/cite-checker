# Notes on training and prompting practice

The Transformer achieved 28.4 BLEU on the WMT 2014 English-to-German translation task [@vaswani2017].
The big Transformer model was trained for 12 days on eight P100 GPUs [@vaswani2017].

BERT obtained a test F1 score of 93.2 on SQuAD v1.1 [@devlin2019].
BERT was pre-trained on English Wikipedia alone [@devlin2019].

An ensemble of residual networks achieved 3.57% top-5 error on the ImageNet test set [@he2016].
Residual networks were shown to outperform Transformers on machine translation [@he2016].
Adam combines momentum with per-parameter adaptive learning rates [@he2016].

Batch normalization matched the accuracy of a strong image classifier with 14 times fewer training steps [@ioffe2015].
An ensemble of batch-normalized networks reached 1.2% top-5 validation error on ImageNet [@ioffe2015].

The default Adam settings are a step size of 0.001, beta1 of 0.9, beta2 of 0.999 and epsilon of 1e-8 [@kingma2015].

Adding "Let's think step by step" raised InstructGPT accuracy on MultiArith from 17.7% to 78.7% [@kojima2022].
The same zero-shot prompt raised GSM8K accuracy from 10.4% to 70.1% [@kojima2022].

Chain-of-thought prompting of PaLM 540B achieved state-of-the-art accuracy on GSM8K [@wei2022].
Chain-of-thought prompting improves reasoning just as much in models under 10 billion parameters [@wei2022].

Recursive attention calibration cut retrieval latency by 40% [@smith2023].
