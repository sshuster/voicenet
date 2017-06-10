# Copyright 2016 ASLP@NPU.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: npuichigo@gmail.com (zhangyuchao)

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# Dependency imports
import argparse
import numpy as np
import os
import sys
import sonnet as snt
import time
import tensorflow as tf
import utils.datasets as datasets

from models.acoustic_model import AcousticModel
from utils.utils import pp, show_all_variables, write_binary_file, ProgressBar

# Basic model parameters as external flags.
FLAGS = None


def restore_from_ckpt(sess, saver):
    ckpt = tf.train.get_checkpoint_state(os.path.join(FLAGS.save_dir, "nnet"))
    if ckpt and ckpt.model_checkpoint_path:
        saver.restore(sess, ckpt.model_checkpoint_path)
        return True
    else:
        tf.logging.fatal("checkpoint not found")
        return False


def train_one_epoch(sess, coord, summary_writer, merged, global_step,
                    train_step, train_loss, train_num_batches):
    if FLAGS.show:
        bar = ProgressBar('Training', max=train_num_batches)

    tr_loss = 0
    for batch in xrange(train_num_batches):
        if FLAGS.show: bar.next()
        if coord.should_stop():
            break
        if batch % 50 == 49:
            _, loss, summary, step = sess.run([train_step, train_loss,
                                               merged, global_step])
            summary_writer.add_summary(summary, step)
        else:
            _, loss = sess.run([train_step, train_loss])
        tr_loss += loss

    if FLAGS.show: bar.finish()

    tr_loss /= train_num_batches
    return tr_loss


def eval_one_epoch(sess, coord, valid_loss, valid_num_batches):
    if FLAGS.show:
        bar = ProgressBar('Validation', max=valid_num_batches)

    val_loss = 0
    for batch in xrange(valid_num_batches):
        if FLAGS.show: bar.next()
        if coord.should_stop():
            break
        loss = sess.run(valid_loss)
        val_loss += loss

    if FLAGS.show: bar.finish()

    val_loss /= valid_num_batches
    return val_loss


def train():
    """Run the training of the acoustic model."""

    dataset_train = datasets.SequenceDataset(
        subset="train",
        config_dir=FLAGS.config_dir,
        data_dir=FLAGS.data_dir,
        batch_size=FLAGS.batch_size,
        input_size=FLAGS.input_dim,
        output_size=FLAGS.output_dim,
        num_enqueuing_threads=FLAGS.num_threads,
        num_epochs=FLAGS.max_epochs,
        infer=False,
        name="dataset_train")

    dataset_valid = datasets.SequenceDataset(
        subset="valid",
        config_dir=FLAGS.config_dir,
        data_dir=FLAGS.data_dir,
        batch_size=FLAGS.batch_size,
        input_size=FLAGS.input_dim,
        output_size=FLAGS.output_dim,
        num_enqueuing_threads=FLAGS.num_threads,
        num_epochs=FLAGS.max_epochs + 1,
        infer=False,
        name="dataset_valid")

    model = AcousticModel(
        rnn_cell=FLAGS.rnn_cell,
        num_hidden=FLAGS.num_hidden,
        dnn_depth=FLAGS.dnn_depth,
        rnn_depth=FLAGS.rnn_depth,
        output_size=FLAGS.output_dim,
        bidirectional=FLAGS.bidirectional,
        rnn_output=FLAGS.rnn_output,
        cnn_output=FLAGS.cnn_output,
        look_ahead=FLAGS.look_ahead,
        name="acoustic_model")

    # Build the training model and get the training loss.
    train_input_sequence, train_target_sequence, train_length = dataset_train()
    train_output_sequence_logits, train_final_state = model(
        train_input_sequence, train_length)
    train_loss = model.cost(
        train_output_sequence_logits, train_target_sequence, train_length)
    tf.summary.scalar('train_loss', train_loss)

    # Get the validation loss.
    valid_input_sequence, valid_target_sequence, valid_length = dataset_valid()
    valid_output_sequence_logits, valid_final_state = model(
        valid_input_sequence, valid_length)
    valid_loss = model.cost(
        valid_output_sequence_logits, valid_target_sequence, valid_length)

    # Set up optimizer with global norm clipping.
    trainable_variables = tf.trainable_variables()
    grads, _ = tf.clip_by_global_norm(
        tf.gradients(train_loss, trainable_variables),
        FLAGS.max_grad_norm)

    learning_rate = tf.get_variable(
        "learning_rate",
        shape=[],
        dtype=tf.float32,
        initializer=tf.constant_initializer(FLAGS.learning_rate),
        trainable=False)
    reduce_learning_rate = learning_rate.assign(
        learning_rate * FLAGS.reduce_learning_rate_multiplier)

    global_step = tf.get_variable(
        name="global_step",
        shape=[],
        dtype=tf.int64,
        initializer=tf.zeros_initializer(),
        trainable=False,
        collections=[tf.GraphKeys.GLOBAL_VARIABLES, tf.GraphKeys.GLOBAL_STEP])

    optimizer = tf.train.AdamOptimizer(learning_rate)
    train_step = optimizer.apply_gradients(
        zip(grads, trainable_variables),
        global_step=global_step)

    show_all_variables()
    merged_all = tf.summary.merge_all()
    saver = tf.train.Saver(trainable_variables, max_to_keep=FLAGS.max_epochs)

    # Train.
    config = tf.ConfigProto()
    # prevent exhausting all the gpu memories
    config.gpu_options.allow_growth = True
    with tf.Session(config=config) as sess:
        # Run init
        sess.run(tf.group(tf.global_variables_initializer(),
                          tf.local_variables_initializer()))

        summary_writer = tf.summary.FileWriter(
            os.path.join(FLAGS.save_dir, "nnet"), sess.graph)

        if FLAGS.resume_training:
            restore_from_ckpt(sess, saver)

        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coord)

        try:
            # add a blank line for log readability
            print()
            sys.stdout.flush()

            loss_prev = eval_one_epoch(sess, coord, valid_loss, dataset_valid.num_batches)
            tf.logging.info("CROSSVAL PRERUN AVG.LOSS %.4f\n" % loss_prev)


            for epoch in xrange(FLAGS.max_epochs):
                # Train one epoch
                time_start = time.time()
                tr_loss = train_one_epoch(sess, coord, summary_writer, merged_all, global_step,
                                          train_step, train_loss, dataset_train.num_batches)
                time_end = time.time()
                used_time = time_end - time_start

                # Validate one epoch
                val_loss = eval_one_epoch(sess, coord, valid_loss, dataset_valid.num_batches)

                # Determine checkpoint path
                FLAGS.learning_rate = sess.run(learning_rate)
                cptk_name = 'nnet_iter%d_lrate%g_tr%.4f_cv%.4f' % (
                    epoch + 1, FLAGS.learning_rate, tr_loss, val_loss)
                checkpoint_path = os.path.join(FLAGS.save_dir, "nnet", cptk_name)

                # Relative loss between previous and current val_loss
                rel_impr = (loss_prev - val_loss) / loss_prev

                # accept or reject new parameters
                if rel_impr > FLAGS.reduce_learning_rate_impr:
                    saver.save(sess, checkpoint_path)
                    # logging training loss along with validation loss
                    tf.logging.info(
                        "ITERATION %d: TRAIN AVG.LOSS %.4f, (lrate%g) "
                        "CROSSVAL AVG.LOSS %.4f, TIME USED %.2f, %s" % (
                            epoch + 1, tr_loss, FLAGS.learning_rate, val_loss,
                            used_time, "nnet accepted"))
                    loss_prev = val_loss
                else:
                    tf.logging.info(
                        "ITERATION %d: TRAIN AVG.LOSS %.4f, (lrate%g) "
                        "CROSSVAL AVG.LOSS %.4f, TIME USED %.2f, %s" % (
                            epoch + 1, tr_loss, FLAGS.learning_rate, val_loss,
                            used_time, "nnet rejected"))
                    restore_from_ckpt(sess, saver)
                    # Reducing learning rate.
                    sess.run(reduce_learning_rate)

                # add a blank line for log readability
                print()
                sys.stdout.flush()

        except Exception, e:
            # Report exceptions to the coordinator.
            coord.request_stop(e)
        finally:
            # Terminate as usual.  It is innocuous to request stop twice.
            coord.request_stop()
            coord.join(threads)


def decode():
    """Run the decoding of the acoustic model."""

    with tf.device('/cpu:0'):
        dataset_test = datasets.SequenceDataset(
            subset="test",
            config_dir=FLAGS.config_dir,
            data_dir=FLAGS.data_dir,
            batch_size=1,
            input_size=FLAGS.input_dim,
            output_size=FLAGS.output_dim,
            num_enqueuing_threads=1,
            num_epochs=1,
            infer=True,
            name="dataset_test")

        model = AcousticModel(
            rnn_cell=FLAGS.rnn_cell,
            num_hidden=FLAGS.num_hidden,
            dnn_depth=FLAGS.dnn_depth,
            rnn_depth=FLAGS.rnn_depth,
            output_size=FLAGS.output_dim,
            bidirectional=FLAGS.bidirectional,
            rnn_output=FLAGS.rnn_output,
            cnn_output=FLAGS.cnn_output,
            look_ahead=FLAGS.look_ahead,
            name="acoustic_model")

        # Build the testing model and get test output sequence.
        test_input_sequence, test_length = dataset_test()
        test_output_sequence_logits, test_final_state = model(
            test_input_sequence, test_length)

    show_all_variables()

    trainable_variables = tf.trainable_variables()
    saver = tf.train.Saver(trainable_variables, max_to_keep=FLAGS.max_epochs)

    # Decode.
    with tf.Session() as sess:
        # Run init
        sess.run(tf.group(tf.global_variables_initializer(),
                          tf.local_variables_initializer()))

        if not restore_from_ckpt(sess, saver): sys.exit(-1)

        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coord)

        # Read cmvn to do reverse mean variance normalization
        cmvn = np.load(os.path.join(FLAGS.data_dir, "train_cmvn.npz"))
        try:
            used_time_sum = frames_sum = 0.0
            for batch in xrange(dataset_test.num_batches):
                if coord.should_stop():
                    break
                time_start = time.time()
                logits, frames = sess.run([test_output_sequence_logits,
                                                test_length])
                time_end = time.time()
                used_time = time_end - time_start
                used_time_sum += used_time
                frames_sum += frames[0]
                sequence = logits * cmvn["stddev_labels"] + cmvn["mean_labels"]
                out_dir_name = os.path.join(FLAGS.save_dir, "test", "cmp")
                out_file_name =os.path.basename(
                    dataset_test.tfrecords_lst[batch]).split('.')[0] + ".cmp"
                out_path = os.path.join(out_dir_name, out_file_name)
                write_binary_file(sequence.squeeze(), out_path, with_dim=False)
                tf.logging.info(
                    "writing inferred cmp to %s (%d frames in %f seconds)" % (out_path, frames[0], used_time))
        except Exception, e:
            # Report exceptions to the coordinator.
            coord.request_stop(e)
        finally:
            # Terminate as usual.  It is innocuous to request stop twice.
            tf.logging.info("Done decoding -- epoch limit reached "
                            "(%d frames per second)" % int(frames_sum / used_time_sum))
            coord.request_stop()
            coord.join(threads)


def main(_):
    """Training or decoding according to FLAGS."""
    if FLAGS.decode != True:
        train()
    else:
        decode()


if __name__ == '__main__':
    tf.logging.set_verbosity(tf.logging.INFO)

    def _str_to_bool(s):
        """Convert string to bool (in argparse context)."""
        if s.lower() not in ['true', 'false']:
            raise ValueError('Argument needs to be a '
                             'boolean, got {}'.format(s))
        return {'true': True, 'false': False}[s.lower()]

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--decode',
        default=False,
        help="Flag indicating decoding or training.",
        action="store_true"
    )
    parser.add_argument(
        '--resume_training',
        default=False,
        help="Flag indicating whether to resume training from cptk.",
        action="store_true"
    )
    parser.add_argument(
        '--input_dim',
        type=int,
        default=145,
        help='The dimension of inputs.'
    )
    parser.add_argument(
        '--output_dim',
        type=int,
        default=75,
        help='The dimension of outputs.'
    )
    parser.add_argument(
        '--rnn_cell',
        type=str,
        default='lstm',
        help='Rnn cell types including rnn, gru and lstm.'
    )
    parser.add_argument(
        '--bidirectional',
        type=_str_to_bool,
        default=False,
        help='Whether to use bidirectional layers.'
    )
    parser.add_argument(
        '--dnn_depth',
        type=int,
        default=3,
        help='Number of layers of dnn model.'
    )
    parser.add_argument(
        '--rnn_depth',
        type=int,
        default=2,
        help='Number of layers of rnn model.'
    )
    parser.add_argument(
        '--num_hidden',
        type=int,
        default=256,
        help='Number of hidden units to use.'
    )
    parser.add_argument(
        '--max_grad_norm',
        type=float,
        default=5.0,
        help='The max gradient normalization.'
    )
    parser.add_argument(
        '--rnn_output',
        type=_str_to_bool,
        default=False,
        help='Whether to use rnn as the output layer.'
    )
    parser.add_argument(
        '--cnn_output',
        type=_str_to_bool,
        default=False,
        help='Whether to use cnn as the output layer.'
    )
    parser.add_argument(
        '--look_ahead',
        type=int,
        default=5,
        help='Number of steps to look ahead in cnn output layer.',
    )
    parser.add_argument(
        '--batch_size',
        type=int,
        default=32,
        help='Mini-batch size.'
    )
    parser.add_argument(
        '--learning_rate',
        type=float,
        default=0.001,
        help='Initial learning rate.'
    )
    parser.add_argument(
        '--max_epochs',
        type=int,
        default=30,
        help='Max number of epochs to run trainer totally.',
    )
    parser.add_argument(
        '--reduce_learning_rate_multiplier',
        type=float,
        default=0.5,
        help='Factor for reducing learning rate.'
    )
    parser.add_argument(
        '--reduce_learning_rate_impr',
        type=float,
        default=0.0,
        help='Reduce and retrain when relative loss is lower than certain improvement.'
    )
    parser.add_argument(
        '--num_threads',
        type=int,
        default=8,
        help='The num of threads to read tfrecords files.'
    )
    parser.add_argument(
        '--save_dir',
        type=str,
        default='exp/acoustic/',
        help='Directory to put the training result.'
    )
    parser.add_argument(
        '--data_dir',
        type=str,
        default='data/',
        help='Directory of train, val and test data.'
    )
    parser.add_argument(
        '--config_dir',
        type=str,
        default='config/',
        help='Directory to load train, val and test lists.'
    )
    parser.add_argument(
        '--show',
        type=_str_to_bool,
        default=True,
        help='Whether to use progress bar.'
    )
    FLAGS, unparsed = parser.parse_known_args()
    pp.pprint(FLAGS.__dict__)
    tf.app.run(main=main, argv=[sys.argv[0]] + unparsed)