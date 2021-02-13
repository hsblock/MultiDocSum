import torch
import torch.optim as optim
from torch.nn.utils import clip_grad_norm_


def build_optim(args, model, checkpoint):
    optimizer = Optimizer(
        args.optim, args.lr, args.max_grad_norm,
        beta1=args.beta1, beta2=args.beta2,
        decay_method=args.decay_method,
        warmup_steps=args.warmup_steps, model_size=args.hidden_size
    )

    optimizer.set_parameters(list(model.named_parameters()))

    if args.checkpoint != '':
        optimizer.optimizer.load_state_dict(checkpoint['optim'])
        if args.visible_gpus != '-1':
            for state in optimizer.optimizer.state.values():
                for k, v in state.items():
                    if torch.is_tensor(v):
                        state[k] = v.cuda()
        if len(optimizer.optimizer.state) < 1:
            raise RuntimeError("Error: loaded Adam optimizer from existing model "
                               "but optimizer state is empty")

    return optimizer


class Optimizer(object):

    def __init__(self, method='adam', learning_rate=3, max_grad_norm=0,
                 lr_decay=1, start_decay_steps=None, decay_steps=None,
                 beta1=0.9, beta2=0.999, adagrad_accum=0.0,
                 decay_method=None, warmup_steps=4000, model_size=None):
        self.method = method
        self.learning_rate = learning_rate
        self.original_lr = learning_rate
        self.max_grad_norm = max_grad_norm
        self.lr_decay = lr_decay
        self.start_decay_steps = start_decay_steps
        self.decay_steps = decay_steps
        self._step = 0
        self.betas = (beta1, beta2)
        self.adagrad_accum = adagrad_accum
        self.decay_method = decay_method
        self.warmup_steps = warmup_steps
        self.model_size = model_size

        self.last_ppl = None
        self.start_decay = False

        self.params = []
        self.sparse_params = []

        self.optimizer = None

    def set_parameters(self, params):
        for k, p in params:
            if p.requires_grad:
                self.params.append(p)
        self.optimizer = optim.Adam(self.params, lr=self.learning_rate, betas=self.betas, eps=1e-9)

    def _set_rate(self, learning_rate):
        self.learning_rate = learning_rate
        self.optimizer.param_groups[0]['lr'] = self.learning_rate

    def step(self):
        self._step += 1
        if self.decay_method == "noam":
            self._set_rate(
                self.original_lr *
                (self.model_size ** -0.5 *
                 min(self._step ** -0.5, self._step * self.warmup_steps ** -1.5)))
        else:
            if self.start_decay_steps is not None and self._step > self.start_decay_steps:
                self.start_decay = True
            if self.start_decay:
                if (self._step - self.start_decay_steps) % self.decay_steps == 0:
                    self.learning_rate = self.learning_rate * self.lr_decay

        self.optimizer.param_groups[0]['lr'] = self.learning_rate

        if self.max_grad_norm:
            clip_grad_norm_(self.params, self.max_grad_norm)
        self.optimizer.step()