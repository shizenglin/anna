import sys
import os
import cPickle as pickle

import numpy
import theano.tensor as T
import theano

from theano.tensor.signal.conv import conv2d as sconv2d
from theano.tensor.signal.downsample import max_pool_2d
from theano.tensor.nnet.conv import conv2d
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams

srng = RandomStreams()


class data_order(object):
    _MINIBATCH_SIZE = 'minibatch-size'
    _WIDTH = 'width'
    _HEIGHT = 'height'
    _CHANNELS = 'num-channels'
    # data order, bc01 type used by theano
    type1 = (_MINIBATCH_SIZE, _CHANNELS, _WIDTH, _HEIGHT)
    # data order, c01b type used by cuda_convnet
    type2 = (_CHANNELS, _WIDTH, _HEIGHT, _MINIBATCH_SIZE)

# nonlinearities

sigmoid = T.nnet.sigmoid

tanh = T.tanh


def identity(x):
    # To create a linear layer.
    return x


def rectify(x):
    return T.maximum(x, 0.0)


def trec(x):
    return x*(x > 1)


def softmax(x):
    return T.nnet.softmax(x)


def all_layers(layer):
    """
    Recursive function to gather all layers below the given layer (including
    the given layer)
    """
    if isinstance(layer, InputLayer) or isinstance(layer, Input2DLayer):
        return [layer]
    elif isinstance(layer, ConcatenateLayer):
        return sum([all_layers(i) for i in layer.input_layers], [layer])
    else:
        return [layer] + all_layers(layer.input_layer)


def all_parameters(layer):
    """
    Recursive function to gather all parameters, starting from the output layer
    """
    if isinstance(layer, InputLayer) or isinstance(layer, Input2DLayer):
        return []
    elif isinstance(layer, ConcatenateLayer):
        return sum([all_parameters(i) for i in layer.input_layers], [])
    else:
        return layer.params + all_parameters(layer.input_layer)


def all_trainable_parameters(layer):
    """
    Recursive function to gather all training parameters, starting from the
    output layer
    """
    if isinstance(layer, InputLayer) or isinstance(layer, Input2DLayer):
        return []
    elif isinstance(layer, ConcatenateLayer):
        return sum([all_trainable_parameters(i) for i in layer.input_layers],
                   [])
    else:
        if layer.trainable:
            return layer.params + all_trainable_parameters(layer.input_layer)
        else:
            return [] + all_trainable_parameters(layer.input_layer)


def all_bias_parameters(layer):
    """
    Recursive function to gather all bias parameters, starting from the output
    layer
    """
    if isinstance(layer, InputLayer) or isinstance(layer, Input2DLayer):
        return []
    elif isinstance(layer, ConcatenateLayer):
        return sum([all_bias_parameters(i) for i in layer.input_layers], [])
    else:
        return layer.bias_params + all_bias_parameters(layer.input_layer)


def all_non_bias_parameters(layer):
    return [p for p in all_parameters(layer)
            if p not in all_bias_parameters(layer)]


def gather_rescaling_updates(layer, c):
    """
    Recursive function to gather weight rescaling updates when the constant is
    the same for all layers.
    """
    if isinstance(layer, InputLayer) or isinstance(layer, Input2DLayer):
        return []
    elif isinstance(layer, ConcatenateLayer):
        return sum([gather_rescaling_updates(i, c)
                    for i in layer.input_layers], [])
    else:
        if hasattr(layer, 'rescaling_updates'):
            updates = layer.rescaling_updates(c)
        else:
            updates = []
        return updates + gather_rescaling_updates(layer.input_layer, c)


def get_param_values(layer):
    params = all_parameters(layer)
    return [p.get_value() for p in params]


def set_param_values(layer, param_values):
    params = all_parameters(layer)
    for p, pv in zip(params, param_values):
        p.set_value(pv)


def reset_all_params(layer):
    for l in all_layers(layer):
        if hasattr(l, 'reset_params'):
            l.reset_params()


def gen_updates_regular_momentum(loss, all_parameters, learning_rate, momentum,
                                 weight_decay):
    all_grads = [theano.grad(loss, param) for param in all_parameters]
    updates = []
    for param_i, grad_i in zip(all_parameters, all_grads):
        mparam_i = theano.shared(param_i.get_value()*0.)
        v = (momentum * mparam_i - weight_decay * learning_rate * param_i
             - learning_rate * grad_i)
        updates.append((mparam_i, v))
        updates.append((param_i, param_i + v))
    return updates


# using the alternative formulation of nesterov momentum described at
# https://github.com/lisa-lab/pylearn2/pull/136
# such that the gradient can be evaluated at the current parameters.
def gen_updates_nesterov_momentum(loss, all_parameters, learning_rate,
                                  momentum, weight_decay):
    all_grads = [theano.grad(loss, param) for param in all_parameters]
    updates = []
    for param_i, grad_i in zip(all_parameters, all_grads):
        mparam_i = theano.shared(param_i.get_value()*0.)
        full_grad = grad_i + weight_decay * param_i
        # new momemtum
        v = momentum * mparam_i - learning_rate * full_grad
        # new parameter values
        w = param_i + momentum * v - learning_rate * full_grad
        updates.append((mparam_i, v))
        updates.append((param_i, w))
    return updates


def gen_updates_sgd(loss, all_parameters, learning_rate):
    all_grads = [theano.grad(loss, param) for param in all_parameters]
    updates = []
    for param_i, grad_i in zip(all_parameters, all_grads):
        updates.append((param_i, param_i - learning_rate * grad_i))
    return updates


def gen_updates_adagrad(loss, all_parameters, learning_rate=1.0, epsilon=1e-6):
    """
    epsilon is not included in the typical formula,

    See "Notes on AdaGrad" by Chris Dyer for more info.
    """
    all_grads = [theano.grad(loss, param) for param in all_parameters]
    # initialise to zeroes with the right shape
    all_accumulators = [theano.shared(param.get_value()*0.)
                        for param in all_parameters]

    updates = []
    for param_i, grad_i, acc_i in zip(all_parameters, all_grads,
                                      all_accumulators):
        acc_i_new = acc_i + grad_i**2
        updates.append((acc_i, acc_i_new))
        updates.append((param_i, param_i - learning_rate * grad_i
                        / T.sqrt(acc_i_new + epsilon)))

    return updates


def gen_updates_rmsprop(loss, all_parameters, learning_rate=1.0, rho=0.9,
                        epsilon=1e-6):
    """
    epsilon is not included in Hinton's video, but to prevent problems with
    relus repeatedly having 0 gradients, it is included here.

    Watch this video for more info: http://www.youtube.com/watch?v=O3sxAc4hxZU
    (formula at 5:20)

    also check http://climin.readthedocs.org/en/latest/rmsprop.html
    """
    all_grads = [theano.grad(loss, param) for param in all_parameters]
    # initialise to zeroes with the right shape
    all_accumulators = [theano.shared(param.get_value()*0.)
                        for param in all_parameters]

    updates = []
    for param_i, grad_i, acc_i in zip(all_parameters, all_grads,
                                      all_accumulators):
        acc_i_new = rho * acc_i + (1 - rho) * grad_i**2
        updates.append((acc_i, acc_i_new))
        updates.append((param_i, param_i - learning_rate * grad_i
                        / T.sqrt(acc_i_new + epsilon)))

    return updates


def gen_updates_adadelta(loss, all_parameters, learning_rate=1.0, rho=0.95,
                         epsilon=1e-6):
    """
    in the paper, no learning rate is considered (so learning_rate=1.0).
    Probably best to keep it at this value. epsilon is important for the very
    first update (so the numerator does not become 0).

    rho = 0.95 and epsilon=1e-6 are suggested in the paper and reported to work
    for multiple datasets (MNIST, speech).

    see "Adadelta: an adaptive learning rate method" by Matthew Zeiler for more
    info.
    """
    all_grads = [theano.grad(loss, param) for param in all_parameters]
    # initialise to zeroes with the right shape
    all_accumulators = [theano.shared(param.get_value()*0.)
                        for param in all_parameters]
    all_delta_accumulators = [theano.shared(param.get_value()*0.)
                              for param in all_parameters]

    # all_accumulators: accumulate gradient magnitudes
    # all_delta_accumulators: accumulate update magnitudes (recursive!)

    updates = []
    for param_i, grad_i, acc_i, acc_delta_i in zip(all_parameters,
                                                   all_grads,
                                                   all_accumulators,
                                                   all_delta_accumulators):
        acc_i_new = rho * acc_i + (1 - rho) * grad_i**2
        updates.append((acc_i, acc_i_new))

        update_i = (grad_i * T.sqrt(acc_delta_i + epsilon)
                    / T.sqrt(acc_i_new + epsilon))
        updates.append((param_i, param_i - learning_rate * update_i))

        acc_delta_i_new = rho * acc_delta_i + (1 - rho) * update_i**2
        updates.append((acc_delta_i, acc_delta_i_new))

    return updates


def shared_single(dim=2):
    """
    Shortcut to create an undefined single precision Theano shared variable.
    """
    shp = tuple([1] * dim)
    return theano.shared(numpy.zeros(shp, dtype='float32'))


# TODO(tpaine) remove this
def sparse_initialisation(n_inputs, n_outputs, sparsity=0.05, std=0.01):
    """
    sparsity: fraction of the weights to each output unit that should be
    nonzero
    """
    weights = numpy.zeros((n_inputs, n_outputs), dtype='float32')
    size = int(sparsity * n_inputs)
    for k in xrange(n_outputs):
        indices = numpy.arange(n_inputs)
        numpy.random.shuffle(indices)
        indices = indices[:size]
        values = numpy.random.randn(size).astype(numpy.float32) * std
        weights[indices, k] = values

    return weights


class Layer(object):
    def __init__(self):
        raise NotImplementedError(str(type(self)) +
                                  " does not implement this method")

    def get_output_shape(self):
        raise NotImplementedError(str(type(self)) +
                                  " does not implement this method")

    def output(self):
        raise NotImplementedError(str(type(self)) +
                                  " does not implement this method")

    def reset_params(self):
        raise NotImplementedError(str(type(self)) +
                                  " does not implement this method")


class InputLayer(Layer):
    def __init__(self, mb_size, n_features, length):
        self.mb_size = mb_size
        self.n_features = n_features
        self.length = length
        self.input_var = T.tensor3('input')

    def get_output_shape(self):
        return (self.mb_size, self.n_features, self.length)

    def output(self, *args, **kwargs):
        """
        return theano variable
        """
        return self.input_var


class FlatInputLayer(InputLayer):
    def __init__(self, mb_size, n_features):
        self.mb_size = mb_size
        self.n_features = n_features
        self.input_var = T.matrix('input')

    def get_output_shape(self):
        return (self.mb_size, self.n_features)

    def output(self, *args, **kwargs):
        """
        return theano variable
        """
        return self.input_var


class Input2DLayer(Layer):
    def __init__(self, mb_size, n_features, width, height):
        self.mb_size = mb_size
        self.n_features = n_features
        self.width = width
        self.height = height
        self.input_var = T.tensor4('input')

        self.data_order = data_order.type1

    def get_output_shape(self):
        return (self.mb_size, self.n_features, self.width, self.height)

    def output(self, *args, **kwargs):
        return self.input_var


class PoolingLayer(Layer):
    def __init__(self, input_layer, ds_factor, ignore_border=False):
        self.ds_factor = ds_factor
        self.input_layer = input_layer
        self.ignore_border = ignore_border
        self.trainable = True
        self.params = []
        self.bias_params = []
        self.mb_size = self.input_layer.mb_size

    def get_output_shape(self):
        output_shape = list(self.input_layer.get_output_shape())
        if self.ignore_border:
            output_shape[-1] = int(numpy.floor(float(output_shape[-1])
                                   / self.ds_factor))
        else:
            output_shape[-1] = int(numpy.ceil(float(output_shape[-1])
                                   / self.ds_factor))
        return tuple(output_shape)

    def output(self, *args, **kwargs):
        input = self.input_layer.output(*args, **kwargs)
        return max_pool_2d(input, (1, self.ds_factor), self.ignore_border)


class Pooling2DLayer(Layer):
    def __init__(self, input_layer, pool_size, ignore_border=False):
        ''' pool_size is a tuple.
        '''
        self.pool_size = pool_size
        self.input_layer = input_layer
        self.ignore_border = ignore_border
        self.trainable = True
        self.params = []
        self.bias_params = []
        self.mb_size = self.input_layer.mb_size

        self.data_order = data_order.type1

        assert (len(self.input_layer.get_output_shape()) == 4), \
            'Input must have 4 dimensions.'

        assert (self.input_layer.data_order == self.data_order), \
            'Input data order does not match this layer\'s data order.'

    def get_output_shape(self):
        output_shape = list(self.input_layer.get_output_shape())
        if self.ignore_border:
            output_shape[-2] = int(numpy.floor(float(output_shape[-2])
                                   / self.pool_size[0]))
            output_shape[-1] = int(numpy.floor(float(output_shape[-1])
                                   / self.pool_size[1]))
        else:
            output_shape[-2] = int(numpy.ceil(float(output_shape[-2])
                                   / self.pool_size[0]))
            output_shape[-1] = int(numpy.ceil(float(output_shape[-1])
                                   / self.pool_size[1]))
        return tuple(output_shape)

    def output(self, *args, **kwargs):
        input = self.input_layer.output(*args, **kwargs)
        return max_pool_2d(input, self.pool_size, self.ignore_border)


class GlobalPooling2DLayer(Layer):
    """
    Global pooling across the entire feature map, useful in NINs.
    """
    def __init__(self, input_layer, pooling_function='mean',
                 nonlinearity=softmax):
        self.input_layer = input_layer
        self.pooling_function = pooling_function
        self.trainable = True
        self.nonlinearity = nonlinearity

        self.params = []
        self.bias_params = []
        self.mb_size = self.input_layer.mb_size

        self.data_order = data_order.type1

        assert (len(self.input_layer.get_output_shape()) == 4), \
            'Input must have 4 dimensions.'

        assert (self.input_layer.data_order == self.data_order), \
            'Input data order does not match this layer\'s data order.'

    def get_output_shape(self):
        # Removes the last 2 dimensions
        return self.input_layer.get_output_shape()[:2]

    def output(self, *args, **kwargs):
        input = self.input_layer.output(*args, **kwargs)
        if self.pooling_function == 'mean':
            out = input.mean([2, 3])
        elif self.pooling_function == 'max':
            out = input.max([2, 3])
        elif self.pooling_function == 'l2':
            out = T.sqrt((input ** 2).mean([2, 3]))

        return self.nonlinearity(out)


class DenseLayer(Layer):
    def __init__(self, input_layer, n_outputs, weights_std, init_bias_value,
                 nonlinearity=rectify, dropout=0.):
        self.n_outputs = n_outputs
        self.input_layer = input_layer
        self.weights_std = numpy.float32(weights_std)
        self.init_bias_value = numpy.float32(init_bias_value)
        self.nonlinearity = nonlinearity
        self.dropout = dropout
        self.mb_size = self.input_layer.mb_size

        input_shape = self.input_layer.get_output_shape()
        self.n_inputs = int(numpy.prod(input_shape[1:]))
        self.flatinput_shape = (self.mb_size, self.n_inputs)

        self.W = shared_single(2)
        self.b = shared_single(1)
        self.trainable = True
        self.params = [self.W, self.b]
        self.bias_params = [self.b]
        self.reset_params()

    def reset_params(self):
        self.W.set_value(
            numpy.random.randn(self.n_inputs, self.n_outputs).astype(
                numpy.float32) * self.weights_std)
        self.b.set_value(numpy.ones(self.n_outputs).astype(numpy.float32)
                         * self.init_bias_value)

    def get_output_shape(self):
        return (self.mb_size, self.n_outputs)

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        # use the 'dropout_active' keyword argument to disable it at test time.
        # It is on by default.
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)
        if len(self.input_layer.get_output_shape()) > 2:
            input = input.reshape(self.flatinput_shape)

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            input = (input / retain_prob
                     * srng.binomial(input.shape, p=retain_prob,
                                     dtype='int32').astype('float32'))
            # apply the input mask and rescale the input accordingly.
            # By doing this it's no longer necessary to rescale the weights
            # at test time.

        return self.nonlinearity(T.dot(input, self.W)
                                 + self.b.dimshuffle('x', 0))


class DenseNoBiasLayer(Layer):
    def __init__(self, input_layer, n_outputs, weights_std,
                 nonlinearity=rectify, dropout=0.):
        self.n_outputs = n_outputs
        self.input_layer = input_layer
        self.weights_std = numpy.float32(weights_std)
        self.nonlinearity = nonlinearity
        self.dropout = dropout
        self.mb_size = self.input_layer.mb_size

        input_shape = self.input_layer.get_output_shape()
        self.n_inputs = int(numpy.prod(input_shape[1:]))
        self.flatinput_shape = (self.mb_size, self.n_inputs)

        self.W = shared_single(2)
        self.trainable = True
        self.params = [self.W]
        self.reset_params()

    def reset_params(self):
        self.W.set_value(
            numpy.random.randn(self.n_inputs, self.n_outputs).astype(
                numpy.float32) * self.weights_std)

    def get_output_shape(self):
        return (self.mb_size, self.n_outputs)

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        # use the 'dropout_active' keyword argument to disable it at test time.
        # It is on by default.
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)
        if len(self.input_layer.get_output_shape()) > 2:
            input = input.reshape(self.flatinput_shape)

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            input = (input / retain_prob
                     * srng.binomial(input.shape, p=retain_prob,
                                     dtype='int32').astype('float32'))
            # apply the input mask and rescale the input accordingly.
            # By doing this it's no longer necessary to rescale the weights
            # at test time.

        return self.nonlinearity(T.dot(input, self.W))


class Conv2DLayer(Layer):
    def __init__(self,
                 input_layer,
                 n_filters,
                 filter_width,
                 filter_height,
                 weights_std,
                 init_bias_value,
                 nonlinearity=rectify,
                 dropout=0.,
                 dropout_tied=False,
                 border_mode='valid',
                 trainable=True):
        self.n_filters = n_filters
        self.filter_width = filter_width
        self.filter_height = filter_height
        self.input_layer = input_layer
        self.weights_std = numpy.float32(weights_std)
        self.init_bias_value = numpy.float32(init_bias_value)
        self.nonlinearity = nonlinearity
        self.dropout = dropout
        # if this is on, the same dropout mask is applied across the entire
        # input map
        self.dropout_tied = dropout_tied
        self.border_mode = border_mode
        self.mb_size = self.input_layer.mb_size

        self.input_shape = self.input_layer.get_output_shape()
        ' mb_size, n_filters, filter_width, filter_height '

        self.filter_shape = (n_filters, self.input_shape[1], filter_width,
                             filter_height)

        self.trainable = trainable
        self.W = shared_single(4)
        self.b = shared_single(1)
        self.params = [self.W, self.b]
        self.bias_params = [self.b]

        self.data_order = data_order.type1

        assert (len(self.input_layer.get_output_shape()) == 4), \
            'Input must have 4 dimensions.'

        assert (self.input_layer.data_order == self.data_order), \
            'Input data order does not match this layer\'s data order.'

        self.reset_params()

    def reset_params(self):
        self.W.set_value(
            numpy.random.randn(*self.filter_shape).astype(numpy.float32)
            * self.weights_std)
        self.b.set_value(numpy.ones(self.n_filters).astype(numpy.float32)
                         * self.init_bias_value)

    def get_output_shape(self):
        if self.border_mode == 'valid':
            output_width = self.input_shape[2] - self.filter_width + 1
            output_height = self.input_shape[3] - self.filter_height + 1
        elif self.border_mode == 'full':
            output_width = self.input_shape[2] + self.filter_width - 1
            output_height = self.input_shape[3] + self.filter_width - 1
        elif self.border_mode == 'same':
            output_width = self.input_shape[2]
            output_height = self.input_shape[3]
        else:
            raise RuntimeError("Invalid border mode: '%s'" % self.border_mode)

        output_shape = (self.input_shape[0], self.n_filters, output_width,
                        output_height)
        return output_shape

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            if self.dropout_tied:
                # tying of the dropout masks across the entire feature maps,
                # so broadcast across the feature maps.
                mask = srng.binomial(
                    (input.shape[0], input.shape[1]),
                    p=retain_prob,
                    dtype='int32').astype('float32').dimshuffle(0, 1, 'x', 'x')
            else:
                mask = srng.binomial(input.shape, p=retain_prob,
                                     dtype='int32').astype('float32')
                # apply the input mask and rescale the input accordingly.
                # By doing this it's no longer necessary to rescale the weights
                # at test time.
            input = input / retain_prob * mask

        if self.border_mode in ['valid', 'full']:
            conved = conv2d(input,
                            self.W,
                            subsample=(1, 1),
                            image_shape=self.input_shape,
                            filter_shape=self.filter_shape,
                            border_mode=self.border_mode)
        elif self.border_mode == 'same':
            conved = conv2d(input,
                            self.W,
                            subsample=(1, 1),
                            image_shape=self.input_shape,
                            filter_shape=self.filter_shape,
                            border_mode='full')
            shift_x = (self.filter_width - 1) // 2
            shift_y = (self.filter_height - 1) // 2
            conved = conved[:, :, shift_x:self.input_shape[2]
                            + shift_x, shift_y:self.input_shape[3] + shift_y]
        else:
            raise RuntimeError("Invalid border mode: '%s'" % self.border_mode)
        return self.nonlinearity(conved + self.b.dimshuffle('x', 0, 'x', 'x'))


class StridedConv2DLayer(Layer):
    def __init__(self,
                 input_layer,
                 n_filters,
                 filter_width,
                 filter_height,
                 stride_x,
                 stride_y,
                 weights_std,
                 init_bias_value,
                 nonlinearity=rectify,
                 dropout=0.,
                 dropout_tied=False,
                 implementation='convolution'):
        """
        implementation can be:
            - convolution: use conv2d with the subsample parameter
            - unstrided: use conv2d + reshaping so the result is a convolution
                with strides (1, 1)
            - single_dot: use a large tensor product
            - many_dots: use a bunch of tensor products
        """
        self.n_filters = n_filters
        self.filter_width = filter_width
        self.filter_height = filter_height
        self.stride_x = stride_x
        self.stride_y = stride_y
        self.input_layer = input_layer
        self.weights_std = numpy.float32(weights_std)
        self.init_bias_value = numpy.float32(init_bias_value)
        self.nonlinearity = nonlinearity
        self.dropout = dropout
        # if this is on, the same dropout mask is applied to the whole map.
        self.dropout_tied = dropout_tied
        # this controls whether the convolution is computed using theano's op,
        # as a bunch of tensor products, or a single stacked tensor product.
        self.implementation = implementation
        self.mb_size = self.input_layer.mb_size

        self.input_shape = self.input_layer.get_output_shape()
        ' mb_size, n_filters, filter_width, filter_height '

        self.filter_shape = (n_filters, self.input_shape[1], filter_width,
                             filter_height)

        if self.filter_width % self.stride_x != 0:
            raise RuntimeError("""Filter width is not a multiple of the stride
                               in the X direction""")

        if self.filter_height % self.stride_y != 0:
            raise RuntimeError("""Filter height is not a multiple of the stride
                               in the Y direction""")

        self.W = shared_single(4)
        self.b = shared_single(1)
        self.params = [self.W, self.b]
        self.bias_params = [self.b]

        self.data_order = data_order.type1

        assert (len(self.input_layer.get_output_shape()) == 4), \
            'Input must have 4 dimensions.'

        assert (self.input_layer.data_order == self.data_order), \
            'Input data order does not match this layer\'s data order.'

        self.reset_params()

    def reset_params(self):
        self.W.set_value(numpy.random.randn(*self.filter_shape).astype(
            numpy.float32) * self.weights_std)
        self.b.set_value(numpy.ones(self.n_filters).astype(numpy.float32)
                         * self.init_bias_value)

    def get_output_shape(self):
        output_width = (self.input_shape[2] - self.filter_width
                        + self.stride_x) // self.stride_x  # integer division
        output_height = (self.input_shape[3] - self.filter_height
                         + self.stride_y) // self.stride_y  # integer division
        output_shape = (self.input_shape[0], self.n_filters, output_width,
                        output_height)
        return output_shape

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            if self.dropout_tied:
                # tying of the dropout masks across the entire feature maps, so
                # broadcast across the feature maps.
                mask = srng.binomial(
                    (input.shape[0], input.shape[1]),
                    p=retain_prob,
                    dtype='int32').astype('float32').dimshuffle(0, 1, 'x', 'x')
            else:
                mask = srng.binomial(input.shape, p=retain_prob,
                                     dtype='int32').astype('float32')
                # apply the input mask and rescale the input accordingly.
                # By doing this it's no longer necessary to rescale the weights
                # at test time.
            input = input / retain_prob * mask

        output_shape = self.get_output_shape()
        W_flipped = self.W[:, :, ::-1, ::-1]

        # crazy convolution stuff!
        if self.implementation == 'single_dot':
            # one stacked product
            num_steps_x = self.filter_width // self.stride_x
            num_steps_y = self.filter_height // self.stride_y

            padded_width = ((self.input_shape[2] // self.filter_width)
                            * self.filter_width + (num_steps_x - 1)
                            * self.stride_x)
            padded_height = ((self.input_shape[3]
                             // self.filter_height) * self.filter_height
                             + (num_steps_y - 1) * self.stride_y)

            truncated_width = min(self.input_shape[2], padded_width)
            truncated_height = min(self.input_shape[3], padded_height)
            input_truncated = input[:, :, :truncated_width, :truncated_height]

            input_padded_shape = (self.input_shape[0], self.input_shape[1],
                                  padded_width, padded_height)
            input_padded = T.zeros(input_padded_shape)
            input_padded = T.set_subtensor(input_padded[
                :, :, :truncated_width, :truncated_height], input_truncated)

            inputs_x = []
            for num_x in xrange(num_steps_x):
                inputs_y = []
                for num_y in xrange(num_steps_y):
                    # pixel shift in the x direction
                    shift_x = num_x * self.stride_x
                    # pixel shift in the y direction
                    shift_y = num_y * self.stride_y

                    width = ((input_padded_shape[2] - shift_x)
                             // self.filter_width)
                    height = ((input_padded_shape[3] - shift_y)
                              // self.filter_height)

                    r_input_shape = (input_padded_shape[0],
                                     input_padded_shape[1],
                                     width,
                                     self.filter_width,
                                     height,
                                     self.filter_height)

                    r_input = input_padded[
                        :, :, shift_x:width * self.filter_width + shift_x,
                        shift_y:height * self.filter_height + shift_y]
                    r_input = r_input.reshape(r_input_shape)

                    inputs_y.append(r_input)

                inputs_x.append(T.stack(*inputs_y))

            inputs_stacked = T.stack(*inputs_x)
            r_conved = T.tensordot(inputs_stacked,
                                   W_flipped,
                                   numpy.asarray([[3, 5, 7], [1, 2, 3]]))

            r_conved = r_conved.dimshuffle(2, 5, 3, 0, 4, 1)
            conved = r_conved.reshape((r_conved.shape[0],
                                       r_conved.shape[1],
                                       r_conved.shape[2] * r_conved.shape[3],
                                       r_conved.shape[4] * r_conved.shape[5]))

            # remove padding
            conved = conved[:, :, :output_shape[2], :output_shape[3]]

        elif self.implementation == 'many_dots':
            # separate products
            num_steps_x = self.filter_width // self.stride_x
            num_steps_y = self.filter_height // self.stride_y

            conved = T.zeros(output_shape)

            for num_x in xrange(num_steps_x):
                for num_y in xrange(num_steps_y):
                    # pixel shift in the x direction
                    shift_x = num_x * self.stride_x
                    # pixel shift in the y direction
                    shift_y = num_y * self.stride_y

                    width = ((self.input_shape[2] - shift_x)
                             // self.filter_width)
                    height = ((self.input_shape[3] - shift_y)
                              // self.filter_height)

                    # we can safely skip this product, it doesn't contribute to
                    # the final convolution.
                    if (width == 0) or (height == 0):
                        continue

                    r_input_shape = (self.input_shape[0],
                                     self.input_shape[1],
                                     width,
                                     self.filter_width,
                                     height,
                                     self.filter_height)

                    r_input = input[
                        :,
                        :,
                        shift_x:width * self.filter_width + shift_x,
                        shift_y:height * self.filter_height + shift_y]
                    r_input = r_input.reshape(r_input_shape)

                    r_conved = T.tensordot(r_input, W_flipped,
                                           numpy.asarray([[1, 3, 5],
                                                         [1, 2, 3]]))
                    r_conved = r_conved.dimshuffle(0, 3, 1, 2)
                    conved = T.set_subtensor(conved[
                        :,
                        :,
                        num_x::num_steps_x,
                        num_y::num_steps_y], r_conved)

        elif self.implementation == 'unstrided':
            num_steps_x = self.filter_width // self.stride_x
            num_steps_y = self.filter_height // self.stride_y

            # input sizes need to be multiples of the strides, truncate to
            # correct sizes.
            truncated_width = ((self.input_shape[2] // self.stride_x)
                               * self.stride_x)
            truncated_height = ((self.input_shape[3] // self.stride_y)
                                * self.stride_y)
            input_truncated = input[:, :, :truncated_width, :truncated_height]

            r_input_shape = (self.input_shape[0],
                             self.input_shape[1],
                             truncated_width // self.stride_x,
                             self.stride_x,
                             truncated_height // self.stride_y,
                             self.stride_y)

            r_input = input_truncated.reshape(r_input_shape)

            # fold strides into the feature maps dimension
            r_input_folded_shape = (self.input_shape[0],
                                    self.input_shape[1] * self.stride_x
                                    * self.stride_y,
                                    truncated_width // self.stride_x,
                                    truncated_height // self.stride_y)
            r_input_folded = r_input.transpose(
                0, 1, 3, 5, 2, 4).reshape(r_input_folded_shape)

            r_filter_shape = (self.filter_shape[0],
                              self.filter_shape[1],
                              num_steps_x,
                              self.stride_x,
                              num_steps_y,
                              self.stride_y)
            # need to operate on the flipped W here, else things get hairy.
            r_W_flipped = W_flipped.reshape(r_filter_shape)

            # fold strides into the feature maps dimension
            r_filter_folded_shape = (self.filter_shape[0],
                                     self.filter_shape[1] * self.stride_x
                                     * self.stride_y,
                                     num_steps_x,
                                     num_steps_y)
            r_W_flipped_folded = r_W_flipped.transpose(
                0, 1, 3, 5, 2, 4).reshape(r_filter_folded_shape)
            r_W_folded = r_W_flipped_folded[:, :, ::-1, ::-1]  # unflip

            conved = conv2d(r_input_folded,
                            r_W_folded,
                            subsample=(1, 1),
                            image_shape=r_input_folded_shape,
                            filter_shape=r_filter_folded_shape)
            # 'conved' should already have the right shape

        elif self.implementation == 'convolution':
            conved = conv2d(input,
                            self.W,
                            subsample=(self.stride_x, self.stride_y),
                            image_shape=self.input_shape,
                            filter_shape=self.filter_shape)
        else:
            raise RuntimeError("Invalid implementation string: '%s'"
                               % self.implementation)

        return self.nonlinearity(conved + self.b.dimshuffle('x', 0, 'x', 'x'))


class ConcatenateLayer(Layer):
    def __init__(self, input_layers):
        self.input_layers = input_layers
        self.params = []
        self.bias_params = []
        self.mb_size = self.input_layers[0].mb_size

    def get_output_shape(self):
        # this assumes the layers are already flat!
        sizes = [i.get_output_shape()[1] for i in self.input_layers]
        return (self.mb_size, sum(sizes))

    def output(self, *args, **kwargs):
        inputs = [i.output(*args, **kwargs) for i in self.input_layers]
        return T.concatenate(inputs, axis=1)
