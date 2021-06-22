import tensorflow as tf
import numpy as np
from glob import glob
from pathlib import Path
from datetime import datetime
import os

from .skeleton import load_model
from .resnet import Resnet101, ResnetSmall, Resnet18
from .data_tool import parse_samples
from .callbacks import CSVLogger, ModelSaver


def parse_train(serialized, append_latlon=False, discount=False, tmax=None):
    examples = parse_samples(serialized)

    N = tf.cast(tf.shape(examples['H'])[0], tf.int64)
    H,W,C = examples['H'][0], examples['W'][0], examples['C'][0]
    T_max = tmax or examples['T_max'][0]

    patch1 = tf.io.decode_raw(examples['patch1'], tf.float32)
    patch1 = tf.reshape(patch1, (-1, H,W,C))
    patch2 = tf.io.decode_raw(examples['patch2'], tf.float32)
    patch2 = tf.reshape(patch2, (-1, H,W,C))

    labels = tf.one_hot(examples['label'] - 1, tf.cast(T_max - 1, tf.int32))

    if append_latlon:
        lat_cos = tf.math.cos(2*np.pi * tf.linspace(examples['lat_start'], examples['lat_end'], H) / 180)
        lat_sin = tf.math.sin(2*np.pi * tf.linspace(examples['lat_start'], examples['lat_end'], H) / 180)

        lon_cos = tf.math.cos(2*np.pi * tf.linspace(examples['long_start'], examples['long_end'], W) / 360)
        lon_sin = tf.math.sin(2*np.pi * tf.linspace(examples['long_start'], examples['long_end'], W) / 360)

        lat_cos = tf.tile(tf.transpose(lat_cos[:,:,None,None], [1,0,2,3]), [1, 1, W, 1])
        lat_sin = tf.tile(tf.transpose(lat_sin[:,:,None,None], [1,0,2,3]), [1, 1, W, 1])

        lon_cos = tf.tile(tf.transpose(lon_cos[:,:,None,None], [1,2,0,3]), (1, H, 1, 1))
        lon_sin = tf.tile(tf.transpose(lon_sin[:,:,None,None], [1,2,0,3]), (1, H, 1, 1))

        patch1 = tf.concat([patch1, lat_cos, lat_sin, lon_cos, lon_sin], axis=-1)
        patch2 = tf.concat([patch2, lat_cos, lat_sin, lon_cos, lon_sin], axis=-1)

    if discount:
        weights = tf.where(examples['label'] <= (T_max // 2), 1.9, 0.1)  # scale by 1.9 to get comparable losses
    else:
        weights = tf.broadcast_to(1.0, (N,))

    X = {'img1': patch1, 'img2': patch2}
    y = labels

    return X, y, weights


def make_train_ds(files, batch_size, n_shuffle=1000, compression_type='ZLIB', append_latlon=False, discount=False, tmax=None):
    assert files

    ds = tf.data.TFRecordDataset(files, num_parallel_reads=4, compression_type=compression_type)
    
    if n_shuffle:
        ds = ds.shuffle(n_shuffle)

    ds = ds.batch(1)
    ds = ds.map(lambda x: parse_train(x, append_latlon, discount, tmax))
    ds = ds.unbatch()
    ds = ds.filter(lambda X, y, weight: tf.math.reduce_sum(y) == 1)
    ds = ds.batch(batch_size)
    
    return ds.prefetch(None)


class Train:

    def __init__(self):
        self.data_path_train = sorted(glob('/data2/stengel/HR/rplearn_train_1979_1990.*.tfrecords'))
        self.data_path_eval = sorted(glob('/data2/stengel/HR/rplearn_eval_2000_2002.*.tfrecords'))
        self.val_freq = 3
        self.tmax = 20

        self.resnet = ResnetSmall((160,160,2), self.tmax, output_logits=False, shortcut='projection')
        #resnet = Resnet18((160,160,2), 16, output_logits=False, shortcut='projection')
        #resnet = Resnet101((160,160,2), 16, output_logits=False)

        self.model_dir = Path('/data/repr_models_HR')
        self.prefix = 'resnet-small-20c'
        self.description = '''
        # Model:
        Our small resnet architecture, 4 blocks with filters [16,32,64,128] and 6 residual blocks in each.
        Starts with strided 8x8x16 conv and 3x3 max-pool (stride 2) as in resnet.

        tail consists of two 3x3x128 convs with BN

        l2-reg:     1e-4
        batch-size: 128
        activation: relu
        initializer: he-normal

        # Data:
        full-res 160x160 patches with 2.5d lookahead
        20 patches per image, 1979-1990 (12 years) -> 0.7 million patches (we reduced this artificially for comparability)            
        eval on 2000-2002

        # Input vars:
        divergence (log1p), relative_vorticity (log1p)

        # Training:
        SGD with momentum=0.9
        lr gets reduced on plateau by one order of magnitude, starting with 1e-1

        '''

        self.start_time = datetime.today()
        self.checkpoint_dir = self.model_dir / '{}_{}'.format(self.prefix, self.start_time.strftime('%Y-%m-%d_%H%M'))

        self.setup_ds()


    def setup_dir(self):
        os.makedirs(self.checkpoint_dir)
        if self.description:
            with open(self.checkpoint_dir / 'description.txt', 'w') as f:
                f.write(self.description)

        self.resnet.summary()
        with open(self.checkpoint_dir / 'model_summary.txt', 'w') as f:
            self.resnet.summary(f)


    def setup_ds(self):
        self.train_ds = make_train_ds(self.data_path_train, 128, n_shuffle=2000, tmax=self.tmax+1)
        self.train_ds = self.train_ds.take(5400)  # for comparability
        self.eval_ds = make_train_ds(self.data_path_eval, 128, n_shuffle=None, tmax=self.tmax+1)


    def train(self):
        loss = tf.keras.losses.CategoricalCrossentropy(from_logits=False),
        metrics = 'categorical_accuracy'

        csv_logger = CSVLogger(self.checkpoint_dir / 'training.csv', keys=['lr', 'loss', 'categorical_accuracy', 'val_loss', 'val_categorical_accuracy'], append=True, separator=' ')
        saver = ModelSaver(self.checkpoint_dir)
        lr_reducer = tf.keras.callbacks.ReduceLROnPlateau('loss', min_delta=4e-2, min_lr=1e-5, patience=6)
        callbacks = [saver, csv_logger]

        optimizer = tf.keras.optimizers.SGD(momentum=0.9, clipnorm=5.0)

        self.resnet.model.compile(
            optimizer=optimizer,
            loss=loss,
            metrics=metrics
        )    

        self.resnet.model.optimizer.learning_rate.assign(1e-1)
        callbacks += [lr_reducer]  # only activate now
        self.resnet.model.fit(
            self.train_ds, 
            validation_data=self.eval_ds, 
            validation_freq=self.val_freq, 
            epochs=110, 
            callbacks=callbacks,
            verbose=2,
            initial_epoch=0
        )


    def run(self):
        self.setup_dir()
        self.train()

    
    def evaluate_single(self, dir, on_train=False):
        model = load_model(dir)

        loss = tf.keras.losses.CategoricalCrossentropy(from_logits=False)
        model.compile(
            loss=loss,
            metrics='categorical_accuracy'
        )  


        ds = self.train_ds if on_train else self.eval_ds
        return model.evaluate(ds, verbose=1)


def main():
    Train().run()
    #Train().evaluate_single('/data/repr_models_HR/resnet-small_2021-06-22_0127/epoch23/', on_train=False)
    

if __name__ == '__main__':
    main()