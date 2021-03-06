#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

import tensorflow as tf
from tensorflow import keras
from tensorflow.python.client import device_lib
import tempfile
import numpy as np
import json
import base64
import argparse

from singa_auto.model import BaseModel, FloatKnob, CategoricalKnob, FixedKnob, utils
from singa_auto.constants import ModelDependency
from singa_auto.model.dev import test_model_class


class TfVgg16(BaseModel):
    '''
    Implements VGG16 on Tensorflow for IMAGE_CLASSIFICATION
    '''

    @staticmethod
    def get_knob_config():
        return {
            'max_epochs': FixedKnob(10),
            'learning_rate': FloatKnob(1e-5, 1e-2, is_exp=True),
            'batch_size': CategoricalKnob([16, 32, 64, 128]),
            'max_image_size': CategoricalKnob([32, 64, 128, 224]),
        }

    def __init__(self, **knobs):
        super().__init__(**knobs)
        self._knobs = knobs
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        self._graph = tf.Graph()
        self._sess = tf.Session(graph=self._graph, config=config)

    def train(self, dataset_path, **kwargs):
        max_image_size = self._knobs.get('max_image_size')
        bs = self._knobs.get('batch_size')
        max_epochs = self._knobs.get('max_epochs')

        utils.logger.log('Available devices: {}'.format(
            str(device_lib.list_local_devices())))

        # Define plot for loss against epochs
        utils.logger.define_plot('Loss Over Epochs',
                                 ['loss', 'early_stop_val_loss'],
                                 x_axis='epoch')

        dataset = utils.dataset.load_dataset_of_image_files(
            dataset_path,
            min_image_size=32,
            max_image_size=max_image_size,
            mode='RGB')
        self._image_size = dataset.image_size
        num_classes = dataset.classes
        (images, classes) = zip(*[(image, image_class)
                                  for (image, image_class) in dataset])
        (images, self._normalize_mean,
         self._normalize_std) = utils.dataset.normalize_images(images)
        images = np.asarray(images)
        classes = np.asarray(keras.utils.to_categorical(classes))

        with self._graph.as_default():
            with self._sess.as_default():
                self._model = self._build_model(num_classes, dataset.image_size)
                self._model.fit(images,
                                classes,
                                epochs=max_epochs,
                                validation_split=0.05,
                                batch_size=bs,
                                callbacks=[
                                    tf.keras.callbacks.EarlyStopping(
                                        monitor='val_loss', patience=2),
                                    tf.keras.callbacks.LambdaCallback(
                                        on_epoch_end=self._on_train_epoch_end)
                                ])

                # Compute train accuracy
                (loss, accuracy) = self._model.evaluate(images, classes)

        utils.logger.log('Train loss: {}'.format(loss))
        utils.logger.log('Train accuracy: {}'.format(accuracy))

    def evaluate(self, dataset_path):
        max_image_size = self._knobs.get('max_image_size')
        dataset = utils.dataset.load_dataset_of_image_files(
            dataset_path,
            min_image_size=32,
            max_image_size=max_image_size,
            mode='RGB')
        (images, classes) = zip(*[(image, image_class)
                                  for (image, image_class) in dataset])
        (images, _, _) = utils.dataset.normalize_images(images,
                                                        self._normalize_mean,
                                                        self._normalize_std)
        images = np.asarray(images)
        classes = keras.utils.to_categorical(classes)
        classes = np.asarray(classes)

        with self._graph.as_default():
            with self._sess.as_default():
                (loss, accuracy) = self._model.evaluate(images, classes)

        utils.logger.log('Validation loss: {}'.format(loss))

        return accuracy

    def predict(self, queries):
        image_size = self._image_size
        images = utils.dataset.transform_images(queries,
                                                image_size=image_size,
                                                mode='RGB')
        (images, _, _) = utils.dataset.normalize_images(images,
                                                        self._normalize_mean,
                                                        self._normalize_std)

        with self._graph.as_default():
            with self._sess.as_default():
                probs = self._model.predict(images)

        return probs.tolist()

    def destroy(self):
        self._sess.close()

    def dump_parameters(self):
        params = {}

        # Save model parameters
        with tempfile.NamedTemporaryFile() as tmp:
            # Save whole model to temp h5 file
            with self._graph.as_default():
                with self._sess.as_default():
                    self._model.save(tmp.name)

            # Read from temp h5 file & encode it to base64 string
            with open(tmp.name, 'rb') as f:
                h5_model_bytes = f.read()

            params['h5_model_base64'] = base64.b64encode(h5_model_bytes).decode(
                'utf-8')

        # Save pre-processing params
        params['image_size'] = self._image_size
        params['normalize_mean'] = json.dumps(self._normalize_mean)
        params['normalize_std'] = json.dumps(self._normalize_std)

        return params

    def load_parameters(self, params):
        # Load model parameters
        h5_model_base64 = params['h5_model_base64']

        with tempfile.NamedTemporaryFile() as tmp:
            # Convert back to bytes & write to temp file
            h5_model_bytes = base64.b64decode(h5_model_base64.encode('utf-8'))
            with open(tmp.name, 'wb') as f:
                f.write(h5_model_bytes)

            # Load model from temp file
            with self._graph.as_default():
                with self._sess.as_default():
                    self._model = keras.models.load_model(tmp.name)

        # Load pre-processing params
        self._image_size = params['image_size']
        self._normalize_mean = json.loads(params['normalize_mean'])
        self._normalize_std = json.loads(params['normalize_std'])

    def _on_train_epoch_end(self, epoch, logs):
        loss = logs['loss']
        early_stop_val_loss = logs['val_loss']
        utils.logger.log(loss=loss,
                         early_stop_val_loss=early_stop_val_loss,
                         epoch=epoch)

    def _build_model(self, num_classes, image_size):
        lr = self._knobs.get('learning_rate')

        model = keras.applications.VGG16(include_top=True,
                                         input_shape=(image_size, image_size,
                                                      3),
                                         weights=None,
                                         classes=num_classes)

        model.compile(optimizer=keras.optimizers.Adam(lr=lr),
                      loss='categorical_crossentropy',
                      metrics=['accuracy'])
        return model


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--train_path',
                        type=str,
                        default='data/cifar10_train.zip',
                        help='Path to train dataset')
    parser.add_argument('--val_path',
                        type=str,
                        default='data/cifar10_val.zip',
                        help='Path to validation dataset')
    parser.add_argument('--test_path',
                        type=str,
                        default='data/cifar10_test.zip',
                        help='Path to test dataset')
    parser.add_argument(
        '--query_path',
        type=str,
        default='examples/data/image_classification/cifar10_test_1.png',
        help='Path(s) to query image(s), delimited by commas')
    (args, _) = parser.parse_known_args()

    queries = utils.dataset.load_images(args.query_path.split(',')).tolist()
    test_model_class(model_file_path=__file__,
                     model_class='TfVgg16',
                     task='IMAGE_CLASSIFICATION',
                     dependencies={ModelDependency.TENSORFLOW: '1.12.0'},
                     train_dataset_path=args.train_path,
                     val_dataset_path=args.val_path,
                     test_dataset_path=args.test_path,
                     queries=queries)
