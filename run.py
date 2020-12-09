import matplotlib.pyplot as plt
import numpy as np
import plac
import pytorch_lightning as pl
import seaborn as sns
import torch
import torch.nn.functional as F
from sklearn.metrics import roc_auc_score, confusion_matrix
from sklearn.utils.class_weight import compute_class_weight
from torch import nn
from torch.utils.data import Dataset, DataLoader
from torch_optimizer import Lookahead

HYPER_D = 16
HYPER_GROUPS = 4

OPT_BATCH_SIZE = 16
OPT_MOMENTUM = 0.9
OPT_LR = 0.01
OPT_WEIGHT_DECAY = 0.001
STOCHASTIC_DEPTH = 0.2

SCHED_EPOCHS = 75
SCHED_GAMMA = 0.5
SCHED_STEP = 15

CFG = [
    {'repeat': 3, 'dim': int(1 * HYPER_D), 'expand': 1, 'stride': 2, 'project': True},
    {'repeat': 4, 'dim': int(2 * HYPER_D), 'expand': 1, 'stride': 2, 'project': True},
    {'repeat': 6, 'dim': int(4 * HYPER_D), 'expand': 1, 'stride': 2, 'project': True},
    {'repeat': 3, 'dim': int(8 * HYPER_D), 'expand': 1, 'stride': 2, 'project': True},
]


def drop_connect(inputs, p, training):
    """Drop connect.

    Args:
        input (tensor: BCWH): Input of this structure.
        p (float: 0.0~1.0): Probability of drop connection.
        training (bool): The running mode.

    Returns:
        output: Output after drop connection.
    """
    assert 0 <= p <= 1, 'p must be in range of [0,1]'

    if not training:
        return inputs

    batch_size = inputs.shape[0]
    keep_prob = 1 - p

    # generate binary_tensor mask according to probability (p for 0, 1-p for 1)
    random_tensor = keep_prob
    random_tensor += torch.rand([batch_size, 1, 1], dtype=inputs.dtype, device=inputs.device)
    binary_tensor = torch.floor(random_tensor)

    output = inputs / keep_prob * binary_tensor
    return output


class OnsetDataset(Dataset):
    def __init__(self, X, y):
        self.X = X
        self.y = y.astype(np.int64)

        if len(self.y.shape) == 2:
            self.w = compute_class_weight(y=np.max(self.y, axis=-1),
                                          class_weight="balanced",
                                          classes=[0, 1])
        else:
            self.w = compute_class_weight(y=self.y,
                                          class_weight="balanced",
                                          classes=[0, 1])

    def __len__(self):
        return len(self.X)

    def __getitem__(self, item):

        X = self.X[item]

        if len(self.y.shape) == 2:
            y_pos = len(np.where(self.y[item] == 1)[0])
            y_neg = len(self.y[item]) - y_pos

            y = 1 if y_pos else 0
            w = self.w[y]

            # Discount loss if either label is over 90% of the sequence
            if y:
                pct_pos = y_pos / (y_pos + y_neg)
                pct_neg = y_neg / (y_pos + y_neg)

                pct = min(pct_pos, pct_neg)
                if pct < 0.1:
                    w *= 10 * pct
                # Make up for discount
                w *= 1.1
        else:
            # When testing we only have a sequence wide label
            y = self.y[item]
            w = self.w[y]

        X = X.transpose((1, 0)).astype(np.float32)
        w = np.array([w]).astype(np.float32)
        return X, y, w


class BlurPool1d(nn.Module):
    def __init__(self, channels, blur_kernel_size: int = 3):
        super().__init__()

        self.channels = channels
        self.blur_kernel_size = blur_kernel_size

        if self.blur_kernel_size == 3:
            binomial = [1, 2, 1]
        elif self.blur_kernel_size == 5:
            binomial = [1, 4, 6, 4, 1]
        elif self.blur_kernel_size == 7:
            binomial = [1, 6, 15, 20, 15, 6, 1]
        else:
            raise ValueError('Supported kernel sizes are in {3, 5, 7}')

        bk = binomial

        bk = bk / np.sum(bk)
        bk = np.repeat(bk, self.channels)
        bk = np.reshape(bk, (self.blur_kernel_size, self.channels, 1))

        # WxCx1 -> Cx1xW
        bk = bk.transpose((1, 2, 0))

        self.kernel = nn.Parameter(torch.from_numpy(bk.astype(np.float32)), requires_grad=False)

    def forward(self, x):
        same = int(self.blur_kernel_size / 2)
        x = F.conv1d(x,
                     weight=self.kernel,
                     padding=same,
                     stride=2,
                     groups=self.channels)
        return x


class ResnetBlock(nn.Module):
    def __init__(self, c_in: int, c_out: int, expand: int, stride: int = 1, project: bool = False):
        super().__init__()

        self.norm1 = nn.BatchNorm1d(num_features=int(c_in), momentum=0.01, eps=0.001)
        self.relu1 = nn.ReLU()
        if stride == 2:
            self.blurpool = BlurPool1d(int(c_in))
        else:
            self.blurpool = None
        self.conv1 = nn.Conv1d(in_channels=c_in,
                               out_channels=int(expand * c_in),
                               kernel_size=3,
                               padding=1,
                               bias=False)

        self.norm2 = nn.BatchNorm1d(num_features=int(expand * c_in), momentum=0.01, eps=0.001)

        self.relu2 = nn.ReLU()
        self.conv2 = nn.Conv1d(in_channels=int(expand * c_in),
                               out_channels=c_out,
                               kernel_size=3,
                               padding=1,
                               bias=False)

        self.stride = stride
        self.project = project

    def forward(self, x, p: float = None):
        x_skip = x

        x = self.norm1(x)
        x = self.relu1(x)
        if self.stride == 2:
            x = self.blurpool(x)
        x = self.conv1(x)
        x = self.norm2(x)
        x = self.relu2(x)
        x = self.conv2(x)

        if self.stride == 2 or self.project:
            return x

        if p:
            x = drop_connect(x, p=p, training=self.training)

        return x + x_skip


class OnsetModule(pl.LightningModule):
    def __init__(self, Xy_train, Xy_valid, Xy_test):
        super().__init__()

        self.X_train, self.y_train = Xy_train
        self.X_valid, self.y_valid = Xy_valid
        self.X_test, self.y_test = Xy_test

        self._test_pred = []
        self._test_true = []

        self.blocks = nn.ModuleList()

        c_in = CFG[0]['dim']
        self.conv_stem = nn.Conv1d(in_channels=10,
                                   out_channels=c_in,
                                   kernel_size=9,
                                   padding=4,
                                   bias=False)

        for cfg in CFG:

            block = ResnetBlock(c_in=c_in,
                                c_out=cfg['dim'],
                                expand=cfg['expand'],
                                stride=cfg['stride'],
                                project=cfg['project'])
            self.blocks.append(block)

            for i in range(1, cfg['repeat']):
                block = ResnetBlock(c_in=cfg['dim'],
                                    c_out=cfg['dim'],
                                    expand=cfg['expand'])
                self.blocks.append(block)

            c_in = cfg['dim']

        self.norm_head = nn.BatchNorm1d(num_features=c_in, momentum=0.01, eps=0.001)
        self.relu_head = nn.ReLU()
        self.pool_head = nn.AdaptiveMaxPool1d((1,))
        self.dropout = nn.Dropout(0.5)
        self.fc = nn.Linear(c_in, 2, bias=True)

    def init(self):
        def _init(m):
            if isinstance(m, nn.Linear):
                torch.nn.init.kaiming_normal_(m.weight)
                if m.bias is not None:
                    torch.nn.init.zeros_(m.bias)
            elif isinstance(m, nn.Conv1d):
                torch.nn.init.kaiming_normal_(m.weight)
                if m.bias is not None:
                    torch.nn.init.zeros_(m.bias)

        self.apply(_init)

    def forward(self, x: torch.tensor):

        # Standardize input preserving relationship between channels
        mu = torch.mean(x, dim=(1, 2)).unsqueeze(dim=-1).unsqueeze(dim=-1)
        sd = torch.std(x, dim=(1, 2)).unsqueeze(dim=-1).unsqueeze(dim=-1)
        if self.training:
            # Add random noise to normalization statistics
            if np.random.random() > 0.5:
                mu *= torch.rand(1, device=x.device) + 1.
            else:
                mu /= torch.rand(1, device=x.device) + 1.

            if np.random.random() > 0.5:
                sd *= torch.rand(1, device=x.device) + 1.
            else:
                sd /= torch.rand(1, device=x.device) + 1.

        x = (x - mu) / sd
        x = self.conv_stem(x)

        depth = 1 + len(self.blocks)

        for idx, block in enumerate(self.blocks):
            x = block(x, p=STOCHASTIC_DEPTH*(idx+1)/depth)

        x = self.norm_head(x)
        x = self.relu_head(x)
        x = self.pool_head(x)[:, :, 0]
        x = self.dropout(x)
        y = self.fc(x)

        return y

    def training_step(self, batch, batch_nb):
        X, y_target, w = batch

        y = self.forward(X)
        loss = torch.mean(w * torch.unsqueeze(F.cross_entropy(y, y_target, reduction="none"), dim=-1))

        self.log('train_loss', loss, prog_bar=False, logger=True)
        self.log('lr', self.optimizers().param_groups[0]['lr'])
        self.log('momentum', self.optimizers().param_groups[0]['momentum'])

        return {'loss': loss}

    def validation_step(self, batch, batch_nb):
        X, y_target, w = batch
        y = self(X)

        loss = torch.mean(w * torch.unsqueeze(F.cross_entropy(y, y_target, reduction="none"), dim=-1))

        self._test_pred.append(F.softmax(y.detach(), dim=-1)[:, 1].cpu().numpy())
        self._test_true.append(y_target.detach().cpu().numpy())

        return {'val_loss': loss}

    def validation_epoch_end(self, outputs):
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()

        self.log('val_loss', avg_loss, prog_bar=False, logger=True)

        self._test_pred = np.concatenate(self._test_pred, axis=0)
        self._test_true = np.concatenate(self._test_true, axis=0)

        try:
            self.log('val_auc', roc_auc_score(self._test_true, self._test_pred))

            cm = confusion_matrix(y_true=self._test_true.astype(np.int),
                                  y_pred=np.round(self._test_pred).astype(np.int),
                                  normalize='true')
            ax = sns.heatmap(cm, annot=True, fmt='.2%', cmap='Blues', vmin=0, vmax=1.)
            ax.set_title('Confusion Matrix - Epoch %d' % self.current_epoch)
            plt.show()

        except ValueError:
            pass

        for name, param in self.named_parameters():
            if param.requires_grad:
                if 'conv' in name or 'fc' in name:
                    self.logger.experiment.add_histogram(name, param, self.global_step)

        self._test_true = []
        self._test_pred = []

    def test_step(self, batch, batch_nb):
        X, y_target, w = batch
        y = self(X)

        loss = torch.mean(w * torch.unsqueeze(F.cross_entropy(y, y_target, reduction="none"), dim=-1))

        self._test_pred.append(F.softmax(y.detach(), dim=-1)[:, 1].cpu().numpy())
        self._test_true.append(y_target.detach().cpu().numpy())

        return {'test_loss': loss, }

    def test_epoch_end(self, outputs):
        avg_loss = torch.stack([x['test_loss'] for x in outputs]).mean()

        self.log('test_loss', avg_loss, prog_bar=False, logger=True)

        self._test_pred = np.concatenate(self._test_pred, axis=0)
        self._test_true = np.concatenate(self._test_true, axis=0)

        try:
            self.log('test_auc', roc_auc_score(self._test_true, self._test_pred))

            cm = confusion_matrix(y_true=self._test_true.astype(np.int),
                                  y_pred=np.round(self._test_pred).astype(np.int),
                                  normalize='true')
            ax = sns.heatmap(cm, annot=True, fmt='.2%', cmap='Blues', vmin=0, vmax=1.)
            ax.set_title('Confusion Matrix - Test')
            plt.show()
        except ValueError:
            pass

        self._test_true = []
        self._test_pred = []

        self._test_true = []
        self._test_pred = []

    def configure_optimizers(self):
        inner_optimizer = torch.optim.SGD(self.parameters(),
                                          lr=OPT_LR,
                                          momentum=OPT_MOMENTUM,
                                          weight_decay=OPT_WEIGHT_DECAY)
        optimizer = Lookahead(inner_optimizer)
        schedule = torch.optim.lr_scheduler.StepLR(optimizer=inner_optimizer,
                                                   step_size=SCHED_STEP,
                                                   gamma=SCHED_GAMMA)

        return [optimizer], [schedule]

    def train_dataloader(self):
        return DataLoader(OnsetDataset(self.X_train, self.y_train),
                          shuffle=True,
                          drop_last=True,
                          batch_size=OPT_BATCH_SIZE)

    def val_dataloader(self):
        return DataLoader(OnsetDataset(self.X_valid, self.y_valid),
                          batch_size=OPT_BATCH_SIZE)

    def test_dataloader(self):
        return DataLoader(OnsetDataset(self.X_test, self.y_test),
                          batch_size=OPT_BATCH_SIZE)


@plac.annotations(seed=('Random seed', 'option', 'S', int))
def main(seed: int = 0):
    pl.seed_everything(seed)

    Xy_train = np.load('data/train.npy', mmap_mode='r')

    X_train = Xy_train[:, :, 1:11]
    y_train = Xy_train[:, :, 0]

    Xy_valid = np.load('data/val.npy', mmap_mode='r')

    X_valid = Xy_valid[:, :, 1:11]
    y_valid = np.max(Xy_valid[:, :, 0], axis=-1)

    X_test = np.swapaxes(np.load('data/test_inps.p', mmap_mode='r'), 1, 2)
    y_test = np.load('data/test_labels.p', mmap_mode='r')

    model = OnsetModule((X_train, y_train),
                        (X_valid, y_valid),
                        (X_test, y_test))
    model.init()

    cb_checkpoint = pl.callbacks.ModelCheckpoint(dirpath='checkpoint',
                                                 monitor='val_auc',
                                                 mode='max',
                                                 verbose=True)
    trainer = pl.Trainer(gpus=1,
                         precision=32,
                         max_epochs=SCHED_EPOCHS,
                         log_every_n_steps=5,
                         flush_logs_every_n_steps=1,
                         callbacks=[cb_checkpoint],
                         deterministic=True)
    trainer.fit(model)
    trainer.test(ckpt_path=cb_checkpoint.best_model_path)


if __name__ == '__main__':
    plac.call(main)
