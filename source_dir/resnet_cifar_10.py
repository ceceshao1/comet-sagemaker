from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import tensorflow as tf
import resnet_model

from hooks import CometSessionHook

tf.enable_eager_execution()

INPUT_TENSOR_NAME = "inputs"
SIGNATURE_NAME = "serving_default"

HEIGHT = 32
WIDTH = 32
DEPTH = 3
NUM_CLASSES = 10


def model_fn(features, labels, mode, params):
    NUM_DATA_BATCHES = params.get('NUM_DATA_BATCHES')
    RESNET_SIZE = params.get('RESNET_SIZE')
    BATCH_SIZE = params.get('BATCH_SIZE')
    NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN = 10000 * NUM_DATA_BATCHES

    _MOMENTUM = params.get('MOMENTUM')
    _WEIGHT_DECAY = params.get("WEIGHT_DECAY")

    _BATCHES_PER_EPOCH = NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN / \
        BATCH_SIZE
    _INITIAL_LEARNING_RATE = params.get(
        'INITIAL_LEARNING_RATE') * BATCH_SIZE / 128

    _BATCHES_PER_EPOCH = NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN / BATCH_SIZE

    inputs = features[INPUT_TENSOR_NAME]
    tf.summary.image('images', inputs, max_outputs=6)

    network = resnet_model.cifar10_resnet_v2_generator(
        RESNET_SIZE, NUM_CLASSES)

    inputs = tf.reshape(inputs, [-1, HEIGHT, WIDTH, DEPTH])
    logits = network(inputs, mode == tf.estimator.ModeKeys.TRAIN)

    predictions = {
        'classes': tf.argmax(logits, axis=1),
        'probabilities': tf.nn.softmax(logits, name='softmax_tensor')
    }

    if mode == tf.estimator.ModeKeys.PREDICT:
        export_outputs = {
            SIGNATURE_NAME: tf.estimator.export.PredictOutput(predictions)
        }
        return tf.estimator.EstimatorSpec(mode=mode,
                                          predictions=predictions,
                                          export_outputs=export_outputs)

    # Calculate loss, which includes softmax cross entropy and L2 regularization.
    cross_entropy = tf.losses.softmax_cross_entropy(
        logits=logits, onehot_labels=labels)

    # Create a tensor named cross_entropy for logging purposes.
    tf.identity(cross_entropy, name='cross_entropy')
    tf.summary.scalar('cross_entropy', cross_entropy)

    # Add weight decay to the loss.
    loss = cross_entropy + _WEIGHT_DECAY * tf.add_n(
        [tf.nn.l2_loss(v) for v in tf.trainable_variables()])

    if mode == tf.estimator.ModeKeys.TRAIN:
        global_step = tf.train.get_or_create_global_step()

        # Multiply the learning rate by 0.1 at 100, 150, and 200 epochs.
        boundaries = [int(_BATCHES_PER_EPOCH * epoch)
                      for epoch in [100, 150, 200]]
        values = [_INITIAL_LEARNING_RATE *
                  decay for decay in [1, 0.1, 0.01, 0.001]]
        learning_rate = tf.train.piecewise_constant(
            tf.cast(global_step, tf.int32), boundaries, values)

        # Create a tensor named learning_rate for logging purposes
        tf.identity(learning_rate, name='learning_rate')
        tf.summary.scalar('learning_rate', learning_rate)

        optimizer = tf.train.MomentumOptimizer(
            learning_rate=learning_rate,
            momentum=_MOMENTUM)

        # Batch norm requires update ops to be added as a dependency to the train_op
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            train_op = optimizer.minimize(loss, global_step)
    else:
        train_op = None

    accuracy = tf.metrics.accuracy(
        tf.argmax(labels, axis=1), predictions['classes'])
    metrics = {'accuracy': accuracy}

    # Create a tensor named train_accuracy for logging purposes
    tf.identity(accuracy[1], name='train_accuracy')
    tf.summary.scalar('train_accuracy', accuracy[1])

    comet_hook = CometSessionHook(
        tensors={'loss': loss, 'accuracy': accuracy[1]},
        parameters=params,
        every_n_iter=None,
        every_n_secs=5)

    return tf.estimator.EstimatorSpec(
        mode=mode,
        predictions=predictions,
        loss=loss,
        train_op=train_op,
        training_hooks=[comet_hook],
        eval_metric_ops=metrics)


def serving_input_fn(params):
    inputs = {INPUT_TENSOR_NAME: tf.placeholder(tf.float32, [None, 32, 32, 3])}
    return tf.estimator.export.ServingInputReceiver(inputs, inputs)


def train_input_fn(training_dir, params):
    return _input_from_files(tf.estimator.ModeKeys.TRAIN,
                             num_data_batches=params.get('NUM_DATA_BATCHES'),
                             batch_size=params.get('BATCH_SIZE'),
                             data_dir=training_dir)


def eval_input_fn(training_dir, params):
    return _input_from_files(tf.estimator.ModeKeys.EVAL,
                             num_data_batches=params.get('NUM_DATA_BATCHES'),
                             batch_size=params.get('BATCH_SIZE'),
                             data_dir=training_dir)


def _input_from_files(mode, num_data_batches, batch_size, data_dir):
    """Uses the contrib.data input pipeline for CIFAR-10 dataset.

  Args:
    mode: Standard names for model modes (tf.estimators.ModeKeys).
    batch_size: The number of samples per batch of input requested.
  """
    dataset = _record_dataset(_filenames(mode, num_data_batches, data_dir))

    # For training repeat forever.
    if mode == tf.estimator.ModeKeys.TRAIN:
        dataset = dataset.repeat()

    dataset = dataset.map(_dataset_parser, num_parallel_calls=1)
    dataset.prefetch(2 * batch_size)

    # For training, preprocess the image and shuffle.
    if mode == tf.estimator.ModeKeys.TRAIN:
        dataset = dataset.map(_train_preprocess_fn, num_parallel_calls=1)
        dataset.prefetch(2 * batch_size)

        # Ensure that the capacity is sufficiently large to provide good random
        # shuffling.
        buffer_size = int(10000 * num_data_batches *
                          0.4) + 3 * batch_size
        dataset = dataset.shuffle(buffer_size=buffer_size)

    # Subtract off the mean and divide by the variance of the pixels.
    dataset = dataset.map(
        lambda image, label: (
            tf.image.per_image_standardization(image), label),
        num_parallel_calls=1)
    dataset.prefetch(2 * batch_size)

    # Batch results by up to batch_size, and then fetch the tuple from the
    # iterator.
    iterator = dataset.batch(batch_size).make_one_shot_iterator()
    images, labels = iterator.get_next()

    return {INPUT_TENSOR_NAME: images}, labels


def _train_preprocess_fn(image, label):
    """Preprocess a single training image of layout [height, width, depth]."""
    # Resize the image to add four extra pixels on each side.
    image = tf.image.resize_image_with_crop_or_pad(
        image, HEIGHT + 8, WIDTH + 8)

    # Randomly crop a [HEIGHT, WIDTH] section of the image.
    image = tf.random_crop(image, [HEIGHT, WIDTH, DEPTH])

    # Randomly flip the image horizontally.
    image = tf.image.random_flip_left_right(image)

    return image, label


def _dataset_parser(value):
    """Parse a CIFAR-10 record from value."""
    # Every record consists of a label followed by the image, with a fixed number
    # of bytes for each.
    label_bytes = 1
    image_bytes = HEIGHT * WIDTH * DEPTH
    record_bytes = label_bytes + image_bytes

    # Convert from a string to a vector of uint8 that is record_bytes long.
    raw_record = tf.decode_raw(value, tf.uint8)

    # The first byte represents the label, which we convert from uint8 to int32.
    label = tf.cast(raw_record[0], tf.int32)

    # The remaining bytes after the label represent the image, which we reshape
    # from [depth * height * width] to [depth, height, width].
    depth_major = tf.reshape(raw_record[label_bytes:record_bytes],
                             [DEPTH, HEIGHT, WIDTH])

    # Convert from [depth, height, width] to [height, width, depth], and cast as
    # float32.
    image = tf.cast(tf.transpose(depth_major, [1, 2, 0]), tf.float32)

    return image, tf.one_hot(label, NUM_CLASSES)


def _record_dataset(filenames):
    """Returns an input pipeline Dataset from `filenames`."""
    record_bytes = HEIGHT * WIDTH * DEPTH + 1
    return tf.data.FixedLengthRecordDataset(filenames, record_bytes)


def _filenames(mode, num_data_batches, data_dir):
    """Returns a list of filenames based on 'mode'."""
    data_dir = os.path.join(data_dir, 'cifar-10-batches-bin')

    assert os.path.exists(data_dir), ('Run cifar10_download_and_extract.py first '
                                      'to download and extract the CIFAR-10 data.')

    if mode == tf.estimator.ModeKeys.TRAIN:
        return [
            os.path.join(data_dir, 'data_batch_%d.bin' % i)
            for i in range(1, num_data_batches + 1)
        ]
    elif mode == tf.estimator.ModeKeys.EVAL:
        return [os.path.join(data_dir, 'test_batch.bin')]
    else:
        raise ValueError('Invalid mode: %s' % mode)
