"""Module that contains base classes for our libraries estimators."""

# Disable pylint invalid name since scikit learn uses "X" as variable name for data
# pylint: disable=invalid-name

from abc import abstractmethod
from copy import deepcopy
from typing import Optional

import numpy as np
import torch
from concrete.common.compilation.artifacts import CompilationArtifacts
from concrete.common.compilation.configuration import CompilationConfiguration

from ..quantization import PostTrainingAffineQuantization
from ..torch import NumpyModule


class QuantizedTorchEstimatorMixin:
    """Mixin that provides quantization for a torch module and follows the Estimator API.

    This class should be mixed in with another that provides the full Estimator API. This class
    only provides modifiers for .fit() (with quantization) and .predict() (optionally in FHE)
    """

    def __init__(
        self,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # The quantized module variable appends "_" so that it is not registered as a sklearn
        # parameter. Only training parameters should register, to enable easy cloning of un-trained
        # estimator
        self.quantized_module_ = None

    @property
    @abstractmethod
    def base_estimator_type(self):
        """Get the sklearn estimator that should be trained by the child class."""

    def get_params_for_benchmark(self):
        """Get the parameters to instantiate the sklearn estimator trained by the child class.

        Returns:
            params (dict): dictionary with parameters that will initialize a new Estimator
        """
        return self.get_params()

    @property
    @abstractmethod
    def base_module_to_compile(self):
        """Get the Torch module that should be compiled to FHE."""

    @property
    @abstractmethod
    def n_bits_quant(self):
        """Get the number of quantization bits."""

    def compile(
        self,
        X: np.ndarray,
        compilation_configuration: Optional[CompilationConfiguration] = None,
        compilation_artifacts: Optional[CompilationArtifacts] = None,
        show_mlir: bool = False,
        use_virtual_lib: bool = False,
    ):
        """Compile the model.

        Args:
            X (numpy.ndarray): the unquantized dataset
            compilation_configuration (Optional[CompilationConfiguration]): the options for
                compilation
            compilation_artifacts (Optional[CompilationArtifacts]): artifacts object to fill
                during compilation
            show_mlir (bool): whether or not to show MLIR during the compilation
            use_virtual_lib (bool): whether to compile using the virtual library that allows higher
                bitwidths

        Raises:
            ValueError: if called before the model is trained
        """
        if self.quantized_module_ is None:
            raise ValueError(
                "The classifier needs to be calibrated before compilation,"
                " please call .fit() first!"
            )

        # Quantize the compilation input set using the quantization parameters computed in .fit()
        quantized_numpy_inputset = deepcopy(self.quantized_module_.q_inputs[0])
        quantized_numpy_inputset.update_values(X)

        # Call the compilation backend to produce the FHE inference circuit
        self.quantized_module_.compile(
            quantized_numpy_inputset,
            compilation_configuration=compilation_configuration,
            compilation_artifacts=compilation_artifacts,
            show_mlir=show_mlir,
            use_virtual_lib=use_virtual_lib,
        )

    def fit(self, X, y, **fit_params):
        """Initialize and fit the module.

        If the module was already initialized, by calling fit, the
        module will be re-initialized (unless ``warm_start`` is True). In addition to the
        torch training step, this method performs quantization of the trained torch model.

        Args:
            X : training data, compatible with skorch.dataset.Dataset
                By default, you should be able to pass:
                * numpy arrays
                * torch tensors
                * pandas DataFrame or Series
                * scipy sparse CSR matrices
                * a dictionary of the former three
                * a list/tuple of the former three
                * a Dataset
                If this doesn't work with your data, you have to pass a
                ``Dataset`` that can deal with the data.
            y (numpy.ndarray): labels associated with training data
            **fit_params: additional parameters that can be used during training, these are passed
                to the torch training interface

        Returns:
            self: the trained quantized estimator
        """
        # Reset the quantized module since quantization is lost during refit
        # This will make the .infer() function call into the Torch nn.Module
        # Instead of the quantized module
        self.quantized_module_ = None

        # Call skorch fit that will train the network
        super().fit(X, y, **fit_params)

        # Create corresponding numpy model
        numpy_model = NumpyModule(self.base_module_to_compile, torch.tensor(X[0, ::]))

        # Get the number of bits used in model creation (used to setup pruning)
        n_bits = self.n_bits_quant

        # Quantize with post-training static method, to have a model with integer weights
        post_training_quant = PostTrainingAffineQuantization(n_bits, numpy_model, is_signed=True)
        self.quantized_module_ = post_training_quant.quantize_module(X)
        return self

    # Disable pylint here because we add an additional argument to .predict,
    # with respect to the base class .predict method.
    # pylint: disable=arguments-differ
    def predict(self, X, execute_in_fhe=False):
        """Predict on user provided data.

        Predicts using the quantized clear or FHE classifier

        Args:
            X : input data, a numpy array of raw values (non quantized)
            execute_in_fhe : whether to execute the inference in FHE or in the clear

        Returns:
            y_pred : numpy ndarray with predictions

        Raises:
            ValueError: if the estimator was not yet trained or compiled
        """

        if execute_in_fhe:
            if self.quantized_module_ is None:
                raise ValueError(
                    "The classifier needs to be calibrated before compilation,"
                    " please call .fit() first!"
                )
            if not self.quantized_module_.is_compiled:
                raise ValueError(
                    "The classifier is not yet compiled to FHE, please call .compile() first"
                )

            # Run over each element of X individually and aggregate predictions in a vector
            if X.ndim == 1:
                X = X.reshape((1, -1))
            y_pred = np.zeros((X.shape[0],), np.int32)
            for idx, x in enumerate(X):
                q_x = self.quantized_module_.quantize_input(x).reshape(1, -1)
                y_pred[idx] = self.quantized_module_.forward_fhe.run(q_x).argmax(axis=1)
            return y_pred

        # For prediction in the clear we call the super class which, in turn,
        # will end up calling .infer of this class
        return super().predict(X)

    def fit_benchmark(self, X, y):
        """Fit the quantized estimator and return reference estimator.

        This function returns both the quantized estimator (itself),
        but also a wrapper around the non-quantized trained NN. This is useful in order
        to compare performance between the quantized and fp32 versions of the classifier

        Args:
            X : training data, compatible with skorch.dataset.Dataset
                By default, you should be able to pass:
                * numpy arrays
                * torch tensors
                * pandas DataFrame or Series
                * scipy sparse CSR matrices
                * a dictionary of the former three
                * a list/tuple of the former three
                * a Dataset
                If this doesn't work with your data, you have to pass a
                ``Dataset`` that can deal with the data.
            y (numpy.ndarray): labels associated with training data

        Returns:
            self: the trained quantized estimator
            fp32_model: trained raw (fp32) wrapped NN estimator
        """

        self.fit(X, y)

        # Create a skorch estimator with the same training parameters as this one
        # Follow sklearn.base.clone: deepcopy parameters obtained with get_params()
        # and pass them to the constructor

        # sklearn docs: "Clone does a deep copy of the model in an estimator without actually
        # copying  attached data. It returns a new estimator with the same parameters
        # that has not been fitted on any data."
        # see: https://scikit-learn.org/stable/modules/generated/sklearn.base.clone.html
        new_object_params = self.get_params_for_benchmark()

        for name, param in new_object_params.items():
            new_object_params[name] = deepcopy(param)

        klass = self.base_estimator_type
        module_copy = deepcopy(self.base_module_to_compile)

        # Construct with the fp32 network already trained for this quantized estimator
        # Need to remove the `module` parameter as we pass the trained instance here
        # This key is only present for NeuralNetClassifiers that don't fix the module type
        # Else this key may be removed already, e.g. by the FixedTypeSkorchNeuralNet
        if "module" in new_object_params:
            new_object_params.pop("module")
        fp32_model = klass(module_copy, **new_object_params)

        # Don't fit the new estimator, it is already trained. We just need to call initialize() to
        # signal to the skorch estimator that it is already trained
        fp32_model.initialize()

        return self, fp32_model
