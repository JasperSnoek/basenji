#!/usr/bin/env python
import sys
import time

import numpy as np

from sklearn.metrics import r2_score
import tensorflow as tf


class RNN:
    def __init__(self):
        pass


    def build(self, job):
        ###################################################
        # model parameters and placeholders
        ###################################################
        self.set_params(job)

        # batches
        self.inputs = tf.placeholder(tf.float32, shape=(self.batch_size, self.batch_length, self.seq_depth))
        self.targets = tf.placeholder(tf.float32, shape=(self.batch_size, self.batch_length, self.num_targets))

        # dropout rates
        self.cnn_dropout_ph = []
        for li in range(self.cnn_layers):
            self.cnn_dropout_ph.append(tf.placeholder(tf.float32))
        self.rnn_dropout_ph = []
        for lin in range(self.rnn_layers):
            self.rnn_dropout_ph.append(tf.placeholder(tf.float32))

        ###################################################
        # convolution layers
        ###################################################
        seq_length = self.batch_length
        seq_depth = self.seq_depth

        if self.cnn_layers > 0:
            # reshape
            cinput = tf.reshape(self.inputs, [self.batch_size, 1, seq_length, seq_depth])

            for li in range(self.cnn_layers):
                with tf.variable_scope('cnn%d' % li) as vs:
                    # convolution params
                    stdev = 1./np.sqrt(self.cnn_filters[li]*seq_depth)
                    kernel = tf.Variable(tf.random_uniform([1, self.cnn_filter_sizes[li], seq_depth, self.cnn_filters[li]], minval=-stdev, maxval=stdev), name='kernel')
                    biases = tf.Variable(tf.zeros([self.cnn_filters[li]]), name='bias')

                    # convolution
                    conv = tf.nn.conv2d(cinput, kernel, [1, 1, 1, 1], padding='SAME')
                    cinput = tf.nn.relu(tf.nn.bias_add(conv, biases), name='conv%d'%li)

                    # pooling
                    if self.cnn_pool[li] > 1:
                        cinput = tf.nn.max_pool(cinput, ksize=[1,1,self.cnn_pool[li],1], strides=[1,1,self.cnn_pool[li],1], padding='SAME', name='pool%d'%li)

                    # dropout
                    if self.cnn_dropout[li] > 0:
                        cinput = tf.nn.dropout(cinput, 1.0-self.cnn_dropout[li])

                    # updates size variables
                    seq_length = seq_length // self.cnn_pool[li]
                    seq_depth = self.cnn_filters[li]

            # reshape for RNN
            rinput = tf.reshape(cinput, [self.batch_size, seq_length, seq_depth])

        else:
            rinput = self.inputs

        # update batch buffer to reflect pooling
        pool_ratio = self.batch_length // seq_length
        if self.batch_buffer % pool_ratio != 0:
            print('Please make the batch_buffer %d divisible by the CNN pooling %d' % (self.batch_buffer, pool_ratio), file=sys.stderr)
            exit(1)
        self.batch_buffer_pool = self.batch_buffer // pool_ratio

        ###################################################
        # recurrent layers
        ###################################################
        # initialize norm stabilizer term
        norm_stabilizer = 0

        # move batch_length to the front as a list
        rinput = tf.unpack(tf.transpose(rinput, [1, 0, 2]))

        for li in range(self.rnn_layers):
            with tf.variable_scope('rnn%d' % li) as vs:
                # determine cell
                if self.cell == 'rnn':
                    cell = tf.nn.rnn_cell.BasicRNNCell(self.rnn_units[li], activation=self.activation)
                elif self.cell == 'gru':
                    cell = tf.nn.rnn_cell.GRUCell(self.rnn_units[li], activation=self.activation)
                elif self.cell == 'lstm':
                    cell = tf.nn.rnn_cell.LSTMCell(self.rnn_units[li], state_is_tuple=True, initializer=tf.contrib.layers.xavier_initializer(uniform=True), activation=self.activation)
                else:
                    print('Cannot recognize RNN cell type %s' % self.cell)
                    exit(1)

                # dropout
                if li < len(self.rnn_dropout) and self.rnn_dropout[li] > 0:
                    cell = tf.nn.rnn_cell.DropoutWrapper(cell, output_keep_prob=(1-self.rnn_dropout_ph[li]))

                # run bidirectional
                if self.cnn_layers == 0 and li == 0:
                    outputs, _, _ = tf.nn.bidirectional_rnn(cell, cell, rinput, dtype=tf.float32)
                else:
                    outputs, _, _ = bidirectional_rnn_tied(cell, cell, rinput, dtype=tf.float32)

                # accumulate norm stablizer
                if self.norm_stabilizer[li] > 0:
                    output_norms = tf.sqrt(tf.reduce_sum(tf.square(outputs), reduction_indices=2))
                    output_norms_diff = tf.squared_difference(output_norms[self.batch_buffer_pool:,:], output_norms[:seq_length-self.batch_buffer_pool,:])
                    norm_stabilizer += self.norm_stabilizer[li]*tf.reduce_mean(output_norms_diff)

                # outputs become input to next layer
                rinput = outputs

        ###################################################
        # output layers
        ###################################################
        with tf.variable_scope('out'):
            out_weights = tf.get_variable(name='weights', shape=[2*self.rnn_units[-1], self.num_targets], dtype=tf.float32, initializer=tf.contrib.layers.xavier_initializer(uniform=True))
            out_biases = tf.Variable(tf.zeros(self.num_targets), name='bias')

        # make final predictions
        preds_length = []
        for li in range(self.batch_buffer_pool, seq_length-self.batch_buffer_pool):
            preds_length.append(tf.matmul(outputs[li], out_weights) + out_biases)

        # convert list to tensor
        preds = tf.pack(preds_length)

        # transpose back to batches in front
        self.preds_op = tf.transpose(preds, [1, 0, 2])

        # repeat if pooling
        if pool_ratio > 1:
            self.preds_op = tf.reshape(tf.tile(tf.reshape(self.preds_op, (-1,self.num_targets)), (1,pool_ratio)), (self.batch_size, self.batch_length-2*self.batch_buffer, self.num_targets))

        # print variables
        for v in tf.all_variables():
            print(v.name, v.get_shape())


        ###################################################
        # loss and optimization
        ###################################################
        # take square difference
        sq_diff = tf.squared_difference(self.preds_op, self.targets[:,self.batch_buffer:self.batch_length-self.batch_buffer,:])

        # set any NaN's to zero
        # nan_indexes = tf.is_nan(sq_diff)
        # tens0 = tf.zeros_like(sq_diff)
        # sq_diff = tf.select(nan_indexes, tens0, sq_diff)

        # take the mean
        self.loss_op = tf.reduce_mean(sq_diff) + norm_stabilizer

        # define optimization
        if self.optimization == 'adam':
            self.opt = tf.train.AdamOptimizer(self.learning_rate, beta1=self.adam_beta1, beta2=self.adam_beta2, epsilon=self.adam_eps)
        else:
            print('Cannot recognize optimization algorithm %s' % self.optimization)
            exit(1)

        # clip gradients
        gvs = self.opt.compute_gradients(self.loss_op)
        if self.grad_clip is None:
            clip_gvs = gvs
        else:
            clip_gvs = [(tf.clip_by_value(g, -self.grad_clip, self.grad_clip), v) for g, v in gvs]
        self.step_op = self.opt.apply_gradients(clip_gvs)


    def drop_rate(self):
        ''' Drop the optimizer learning rate. '''
        self.opt._lr /= 2


    def set_params(self, job):
        ''' Set RNN parameters. '''

        ###################################################
        # data attributes
        ###################################################
        self.seq_depth = job.get('seq_depth', 4)
        self.num_targets = job['num_targets']

        ###################################################
        # batching
        ###################################################
        self.batch_size = job.get('batch_size', 64)
        self.batch_length = job.get('batch_length', 1024)
        self.batch_buffer = job.get('batch_buffer', 64)

        ###################################################
        # training
        ###################################################
        self.learning_rate = job.get('learning_rate', 0.001)
        self.adam_beta1 = job.get('adam_beta1', 0.9)
        self.adam_beta2 = job.get('adam_beta2', 0.999)
        self.adam_eps = job.get('adam_eps', 1e-8)
        self.optimization = job.get('optimization', 'adam').lower()
        self.grad_clip = job.get('grad_clip', 1)

        ###################################################
        # CNN params
        ###################################################
        self.cnn_filters = np.atleast_1d(job.get('cnn_filters', []))
        self.cnn_filter_sizes = np.atleast_1d(job.get('cnn_filter_sizes', []))
        self.cnn_layers = len(self.cnn_filters)
        self.cnn_pool = layer_extend(job.get('cnn_pool', []), 1, self.cnn_layers)

        ###################################################
        # RNN params
        ###################################################
        self.rnn_units = np.atleast_1d(job.get('rnn_units', [100]))
        self.rnn_layers = len(self.rnn_units)
        self.cell = job.get('cell', 'lstm').lower()
        self.activation = job.get('activation','tanh').lower()
        if self.activation == 'relu':
            self.activation = tf.nn.relu
        elif self.activation == 'tanh':
            self.activation = tf.tanh
        else:
            print('Activation %s not implemented' % self.activation, file=sys.stderr)
            exit(1)

        ###################################################
        # regularization
        ###################################################
        self.cnn_dropout = layer_extend(job.get('cnn_dropout', []), 0, self.cnn_layers)

        self.rnn_dropout = layer_extend(job.get('rnn_dropout', []), 0, self.rnn_layers)
        self.norm_stabilizer = layer_extend(job.get('norm_stabilizer', []), 0, self.rnn_layers)

        # batch normalization?


    def predict(self, sess, batcher):
        ''' Compute predictions on a test set. '''

        preds = []

        # setup feed dict for dropout
        fd = {}
        for li in range(self.cnn_layers):
            fd[self.cnn_dropout_ph[li]] = 0
        for li in range(self.rnn_layers):
            fd[self.rnn_dropout_ph[li]] = 0

        # get first batch
        Xb, _, Nb = batcher.next()

        while Xb is not None:
            # update feed dict
            fd[self.inputs] = Xb

            # measure batch loss
            preds_batch = sess.run(self.preds_op, feed_dict=fd)

            # accumulate predictions and targets
            preds.append(preds_batch[:Nb])

            # next batch
            Xb, _, Nb = batcher.next()

        # reset batcher
        batcher.reset()

        # accumulate predictions
        preds = np.vstack(preds)

        return preds


    def test(self, sess, batcher, return_preds=False):
        ''' Compute model accuracy on a test set. '''

        batch_losses = []
        preds = []
        targets = []

        # setup feed dict for dropout
        fd = {}
        for li in range(self.cnn_layers):
            fd[self.cnn_dropout_ph[li]] = 0
        for li in range(self.rnn_layers):
            fd[self.rnn_dropout_ph[li]] = 0

        # get first batch
        Xb, Yb, Nb = batcher.next()

        while Xb is not None:
            # update feed dict
            fd[self.inputs] = Xb
            fd[self.targets] = Yb

            # measure batch loss
            preds_batch, loss_batch = sess.run([self.preds_op, self.loss_op], feed_dict=fd)

            # accumulate loss
            batch_losses.append(loss_batch)

            # accumulate predictions and targets
            preds.append(preds_batch[:Nb])
            targets.append(Yb[:Nb,self.batch_buffer:self.batch_length-self.batch_buffer,:])

            # next batch
            Xb, Yb, Nb = batcher.next()

        # reset batcher
        batcher.reset()

        # accumulate predictions and targets
        preds = np.vstack(preds)
        targets = np.vstack(targets)

        # compute R2 per target
        r2 = np.zeros(self.num_targets)
        for ti in range(self.num_targets):
            # flatten
            preds_ti = preds[:,:,ti].flatten()
            targets_ti = targets[:,:,ti].flatten()

            # remove NaN's
            #valid_indexes = np.logical_not(np.isnan(targets_ti))
            #preds_ti = preds_ti[valid_indexes]
            #targets_ti = targets_ti[valid_indexes]

            # compute R2
            tmean = targets_ti.mean(dtype='float64')
            tvar = (targets_ti-tmean).var(dtype='float64')
            pvar = (targets_ti-preds_ti).var(dtype='float64')
            r2[ti] = 1.0 - pvar/tvar
            # print('%d %f %f %f %f' % (ti, tmean, tvar, pvar, r2[ti]))

        if return_preds:
            return np.mean(batch_losses), np.mean(r2), preds
        else:
            return np.mean(batch_losses), np.mean(r2)


    def train_epoch(self, sess, batcher):
        ''' Execute one training epoch '''

        # initialize training loss
        train_loss = []

        # setup feed dict for dropout
        fd = {}
        for li in range(self.cnn_layers):
            fd[self.cnn_dropout_ph[li]] = self.cnn_dropout[li]
        for li in range(self.rnn_layers):
            fd[self.rnn_dropout_ph[li]] = self.rnn_dropout[li]

        # get first batch
        Xb, Yb, Nb = batcher.next()

        while Xb is not None and Nb == self.batch_size:
            # update feed dict
            fd[self.inputs] = Xb
            fd[self.targets] = Yb

            # run step
            loss_batch, _ = sess.run([self.loss_op, self.step_op], feed_dict=fd)

            # accumulate loss
            train_loss.append(loss_batch)

            # next batch
            Xb, Yb, Nb = batcher.next()

        # reset training batcher
        batcher.reset()

        return np.mean(train_loss)


def layer_extend(var, default, layers):
    ''' Process job input to extend for the
         proper number of layers. '''

    # if it's a number
    if type(var) != list:
        # change the default to that number
        default = var

        # make it a list
        var = [var]

    # extend for each layer
    while len(var) < layers:
        var.append(default)

    return var


from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.ops import array_ops
from tensorflow.python.ops.rnn import _reverse_seq
def bidirectional_rnn_tied(cell_fw, cell_bw, inputs,
                      initial_state_fw=None, initial_state_bw=None,
                      dtype=None, sequence_length=None, scope=None):
    name = scope or "BiRNN"
    with vs.variable_scope(name) as fw_scope:
        # Forward direction
        output_fw, output_state_fw = tf.nn.rnn(cell_fw, inputs, initial_state_fw, dtype, sequence_length, scope=fw_scope)

    with vs.variable_scope(name, reuse=True) as bw_scope:
        # Backward direction
        tmp, output_state_bw = tf.nn.rnn(cell_bw, _reverse_seq(inputs, sequence_length),
                 initial_state_bw, dtype, sequence_length, scope=bw_scope)

    output_bw = _reverse_seq(tmp, sequence_length)

    # Concat each of the forward/backward outputs
    outputs = [array_ops.concat(1, [fw, bw]) for fw, bw in zip(output_fw, output_bw)]

    return (outputs, output_state_fw, output_state_bw)
