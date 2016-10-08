#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Giacomo Berardi <giacbrd.com>
# Licensed under the GNU LGPL v3 - http://www.gnu.org/licenses/lgpl.html

from __future__ import division  # py3 "true division"

import logging
import sys

from gensim.models import Word2Vec
from gensim.models.word2vec import train_cbow_pair, Vocab

try:
    from queue import Queue, Empty
except ImportError:
    from Queue import Queue, Empty

from numpy import prod, exp, empty, zeros, ones, float32 as REAL, dot, sum as np_sum

__author__ = 'Giacomo Berardi <giacbrd.com>'

logger = logging.getLogger(__name__)


def train_batch_labeled_cbow(model, sentences, alpha, work=None, neu1=None):
    result = 0
    for sentence in sentences:
        document, target = sentence
        word_vocabs, target_vocabs = [], []
        for w in document:
            if w in model.vocab:
                word_vocabs.append(model.vocab[w])
        for t in target:
            if t in model.lvocab:
                target_vocabs.append(model.lvocab[t])
        for target in target_vocabs:
            word2_indices = [w.index for w in word_vocabs]
            l1 = np_sum(model.syn0[word2_indices], axis=0)  # 1 x vector_size
            if word2_indices and model.cbow_mean:
                l1 /= len(word2_indices)
            train_cbow_pair(model, target, word2_indices, l1, alpha)
        result += len(word_vocabs)
    return result


def score_cbow_labeled_pair(model, target, word2_indices, l1):
    l2a = model.syn1[target.point]  # 2d matrix, codelen x layer1_size
    sgn = (-1.0) ** target.code  # ch function, 0-> 1, 1 -> -1
    prob = 1.0 / (1.0 + exp(-sgn * dot(l1, l2a.T)))
    return prod(prob)


def score_document_labeled_cbow(model, document, label, work=None, neu1=None):
    if model.negative:
        raise RuntimeError("scoring is only available for HS=True")

    word_vocabs, target_vocabs = [], []
    for w in document:
        if w in model.vocab:
            word_vocabs.append(model.vocab[w])

    if label in model.lvocab:
        target = model.lvocab[label]
    else:
        raise KeyError('Class label %s not found in the vocabulary' % label)

    word2_indices = [word2.index for word2 in word_vocabs]
    l1 = np_sum(model.syn0[word2_indices], axis=0)  # 1 x layer1_size
    if word2_indices and model.cbow_mean:
        l1 /= len(word2_indices)
    return score_cbow_labeled_pair(model, target, word2_indices, l1)


class LabeledWord2Vec(Word2Vec):
    def __init__(self, **kwargs):
        """
        Exactly as the parent class `Word2Vec <https://radimrehurek.com/gensim/models/word2vec.html#gensim.models.word2vec.Word2Vec>`_.
        Some parameter values are overwritten (e.g. sg=0 because we never use skip-gram here), look at the code for details.
        """
        self.lvocab = {}  # Vocabulary of labels only
        self.index2label = []
        kwargs['sg'] = 0
        kwargs['window'] = sys.maxint
        kwargs['hs'] = 1
        kwargs['negative'] = 0
        kwargs['sentences'] = None
        super(LabeledWord2Vec, self).__init__(**kwargs)

    def _raw_word_count(self, job):
        """Return the number of words in a given job."""
        return sum(len(sentence[0]) for sentence in job)

    def _do_train_job(self, sentences, alpha, inits):
        """
        Train a single batch of sentences (documents and labels in LabeledWord2Vec).
        Return 2-tuple `(effective word count after ignoring unknown words and sentence length trimming,
        total word count)`.
        """
        work, neu1 = inits
        tally = 0
        if self.sg:
            raise NotImplementedError('Supervised learning in fastText is based only on the CBOW model')
        else:
            tally += train_batch_labeled_cbow(self, sentences, alpha, work, neu1)
        return tally, self._raw_word_count(sentences)

    def build_vocab(self, sentences, labels, keep_raw_vocab=False, trim_rule=None, progress_per=10000):
        """
        Build vocabularies from a sequence of sentences (can be a once-only generator stream) and labels.
        Each sentence and the labels argument must be a list of unicode strings.

        """

        class FakeSelf(LabeledWord2Vec):
            def __init__(self, max_vocab_size, min_count, sample, estimate_memory):
                self.max_vocab_size = max_vocab_size
                self.corpus_count = 0
                self.raw_vocab = None
                self.vocab = {}
                self.min_count = min_count
                self.index2word = []
                self.sample = sample
                self.estimate_memory = estimate_memory

        # Build words and labels vocabularies in two different oobjects
        words_vocab = FakeSelf(self.max_vocab_size, self.min_count, self.sample, self.estimate_memory)
        labels_vocab = FakeSelf(sys.maxint, 0, 0, self.estimate_memory)
        self.__class__.scan_vocab(words_vocab, sentences, progress_per=progress_per, trim_rule=trim_rule)
        self.__class__.scan_vocab(labels_vocab, [labels], progress_per=progress_per, trim_rule=None)
        self.__class__.scale_vocab(words_vocab, keep_raw_vocab=keep_raw_vocab, trim_rule=trim_rule)
        self.__class__.scale_vocab(labels_vocab, keep_raw_vocab=False, trim_rule=None)
        self.corpus_count = words_vocab.corpus_count
        self.raw_vocab = words_vocab.raw_vocab
        self.vocab = words_vocab.vocab
        self.lvocab = labels_vocab.vocab
        self.index2word = words_vocab.index2word
        self.index2label = labels_vocab.index2word
        self.finalize_vocab()  # build tables & arrays

    def finalize_vocab(self):
        """Build tables and model weights based on final vocabulary settings."""

        class FakeSelf(LabeledWord2Vec):
            def __init__(self, vocab):
                self.vocab = vocab

        if self.sorted_vocab:
            self.sort_vocab()
        # add info about each word's Huffman encoding
        self.__class__.create_binary_tree(FakeSelf(self.lvocab))
        if self.null_word:
            # create null pseudo-word for padding when using concatenative L1 (run-of-words)
            # this word is only ever input – never predicted – so count, huffman-point, etc doesn't matter
            word, v = '\0', Vocab(count=1, sample_int=0)
            v.index = len(self.vocab)
            self.index2word.append(word)
            self.vocab[word] = v
        # set initial input/projection and hidden weights
        self.reset_weights()

    def reset_weights(self):
        """Reset all projection weights to an initial (untrained) state, but keep the existing vocabulary."""
        logger.info("resetting layer weights")
        self.syn0 = empty((len(self.vocab), self.vector_size), dtype=REAL)
        # randomize weights vector by vector, rather than materializing a huge random matrix in RAM at once
        for i in xrange(len(self.vocab)):
            # construct deterministic seed from word AND seed argument
            self.syn0[i] = self.seeded_vector(self.index2word[i] + str(self.seed))
        # Output layer is only made of labels
        self.syn1 = zeros((len(self.lvocab), self.layer1_size), dtype=REAL)
        self.syn0norm = None
        self.syn0_lockf = ones(len(self.vocab), dtype=REAL)  # zeros suppress learning

    def reset_from(self, other_model):
        """
        Borrow shareable pre-built structures (like vocab) from the other_model. Useful
        if testing multiple models in parallel on the same corpus.
        """
        self.lvocab = other_model.lvocab
        self.index2label = other_model.index2label
        super(LabeledWord2Vec, self).reset_from(other_model)