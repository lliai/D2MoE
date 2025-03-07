import copy
from collections import defaultdict
from collections.abc import Iterable
from torch.utils.tensorboard import SummaryWriter
from numbers import Number
from module import ntuple


class Logger:
    def __init__(self, path):
        self.path = path
        self.writer = SummaryWriter(self.path)
        self.tracker = defaultdict(int)
        self.counter = defaultdict(int)
        self.mean = defaultdict(int)
        self.history = defaultdict(list)
        self.iterator = defaultdict(int)

    def save(self, flush):
        for name in self.mean:
            if len(self.history[name]) < 1000000:
                self.history[name].append(self.mean[name])
            # record counter for pruned channels
            # if 'pruned_channels' in name:
            #     self.history[f'{name}_counter'] = self.counter[name]
        if flush:
            self.flush()
        return

    def reset(self):
        self.tracker = defaultdict(int)
        self.counter = defaultdict(int)
        self.mean = defaultdict(int)
        return

    def append(self, result, tag, n=1):
        for k in result:
            name = '{}/{}'.format(tag, k)
            self.tracker[name] = result[k]
            prev_n = copy.deepcopy(n)
            if isinstance(result[k], Number):
                self.counter[name] += n
                self.mean[name] = ((self.counter[name] - n) * self.mean[name] + n * result[k]) / self.counter[name]
            elif isinstance(result[k], list) and len(result[k]) > 0:
                if name not in self.mean:
                    self.counter[name] = [0 for _ in range(len(result[k]))]
                    self.mean[name] = [0 for _ in range(len(result[k]))]
                _ntuple = ntuple(len(result[k]))
                n = _ntuple(n)
                for i in range(len(result[k])):
                    if isinstance(result[k][i], Number):
                        self.counter[name][i] += n[i]
                        self.mean[name][i] = ((self.counter[name][i] - n[i]) * self.mean[name][i] + n[i] *
                                              result[k][i]) / self.counter[name][i]
            else:
                print(result[k], type(result[k]), k)
                raise ValueError('Not valid data type for logger')
            n = prev_n
        return

    def accumulate(self, result, tag):
        for k in result:
            name = '{}/{}'.format(tag, k)
            self.tracker[name] = result[k]
            if name not in self.history:
                self.history[name] = []
            if isinstance(result[k], Number):
                self.history[name].append(result[k])
            elif isinstance(result[k], list) and len(result[k]) > 0:
                self.history[name].append(result[k])
                print('name', name, len(self.history[name]))
            else:
                print(result[k], type(result[k]), k)
                raise ValueError('Not valid data type for logger')
        return
    
    def write(self, tag, metric_names):
        names = ['{}/{}'.format(tag, k) for k in metric_names]
        evaluation_info = []
        for name in names:
            tag, k = name.split('/')
            if isinstance(self.mean[name], Number):
                s = self.mean[name]
                evaluation_info.append('{}: {:.4f}'.format(k, s))
                if self.writer is not None:
                    self.iterator[name] += 1
                    self.writer.add_scalar(name, s, self.iterator[name])
            elif isinstance(self.mean[name], Iterable):
                s = tuple(self.mean[name])
                evaluation_info.append('{}: {}'.format(k, s))
                if self.writer is not None:
                    self.iterator[name] += 1
                    self.writer.add_scalar(name, s[0], self.iterator[name])
            else:
                raise ValueError('Not valid data type')
        info_name = '{}/info'.format(tag)
        info = self.tracker[info_name]
        info[2:2] = evaluation_info
        info = '  '.join(info)
        if self.writer is not None:
            self.iterator[info_name] += 1
            self.writer.add_text(info_name, info, self.iterator[info_name])
        return info

    def flush(self):
        self.writer.flush()
        return

    def load_state_dict(self, state_dict):
        self.tracker = state_dict['tracker']
        self.counter = state_dict['counter']
        self.mean = state_dict['mean']
        self.history = state_dict['history']
        self.iterator = state_dict['iterator']
        return

    def state_dict(self):
        return {'tracker': self.tracker, 'counter': self.counter, 'mean': self.mean, 'history': self.history,
                'iterator': self.iterator}


def make_logger(path):
    logger = Logger(path)
    return logger
