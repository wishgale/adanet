"""An AdaNet iteration implementation in Tensorflow using a single graph.

Copyright 2018 The AdaNet Authors. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections

from adanet.core import dict_utils
from adanet.core import subnetwork

import numpy as np
import six
import tensorflow as tf


# TODO: Include estimator_specs instead of candidates.
class _Iteration(
    collections.namedtuple("_Iteration", [
        "number", "candidates", "subnetwork_specs", "estimator_spec",
        "best_candidate_index", "summaries", "is_over_fn", "subnetwork_reports",
        "step"
    ])):
  """An AdaNet iteration.

  An AdaNet iteration represents the simultaneous training of multiple
  candidates for one iteration of the AdaNet loop, and tracks the best
  candidate's loss, predictions, and evaluation metrics.

  There must be maximum one _Iteration per graph.
  """

  def __new__(cls, number, candidates, subnetwork_specs, estimator_spec,
              best_candidate_index, summaries, is_over_fn, subnetwork_reports,
              step):
    """Creates a validated `_Iteration` instance.

    Args:

    Returns:
      A validated `_Iteration` object.

    Args:
      number: The iteration number.
      candidates: List of `_Candidate` instances to track.
      subnetwork_specs: List of `_SubnetworkSpec` instances.
      estimator_spec: `EstimatorSpec` instance.
      best_candidate_index: Int `Tensor` indicating the best candidate's index.
      summaries: List of `adanet.Summary` instances for each candidate.
      is_over_fn: A fn()->Boolean `Variable` indicating if iteration is over.
      subnetwork_reports: Dict mapping string names to `subnetwork.Report`s, one
        per candidate.
      step: Integer `Tensor` representing the step since the beginning of the
        current iteration, as opposed to the global step.

    Raises:
      ValueError: If validation fails.
    """

    if not isinstance(number, (int, np.integer)):
      raise ValueError("number must be an integer")
    if number < 0:
      raise ValueError("number must be greater than 0 got %d" % (number))
    if not isinstance(candidates, list) or not candidates:
      raise ValueError("candidates must be a non-empty list")
    if estimator_spec is None:
      raise ValueError("estimator_spec is required")
    if best_candidate_index is None:
      raise ValueError("best_candidate_index is required")
    if not isinstance(subnetwork_reports, dict):
      raise ValueError("subnetwork_reports must be a dict")
    if step is None:
      raise ValueError("step is required")
    return super(_Iteration, cls).__new__(
        cls,
        number=number,
        candidates=candidates,
        subnetwork_specs=subnetwork_specs,
        estimator_spec=estimator_spec,
        best_candidate_index=best_candidate_index,
        summaries=summaries,
        is_over_fn=is_over_fn,
        subnetwork_reports=subnetwork_reports,
        step=step)


def _is_over_var():
  var = tf.get_variable(
      "is_over_var",
      shape=[],
      initializer=tf.zeros_initializer(),
      trainable=False,
      dtype=tf.bool)
  return var


class _IterationBuilder(object):
  """Builds AdaNet iterations."""

  def __init__(self,
               candidate_builder,
               subnetwork_manager,
               ensemble_builder,
               ensemblers,
               summary_maker,
               replicate_ensemble_in_training=False,
               use_tpu=False):
    """Creates an `_IterationBuilder` instance.

    Args:
      candidate_builder: A `_CandidateBuilder` instance.
      subnetwork_manager: A `_SubnetworkManager` instance.
      ensemble_builder: An `_EnsembleBuilder` instance.
      ensemblers: An iterable of :class:`adanet.ensemble.Ensembler` objects that
        define how to ensemble a group of subnetworks.
      summary_maker: A function that constructs an `adanet.Summary` instance
        from (namespace, scope, and skip_summary).
      replicate_ensemble_in_training: Whether to build the frozen subnetworks in
        `training` mode during training.
      use_tpu: Whether AdaNet is running on TPU.

    Returns:
      An `_IterationBuilder` object.
    """

    self._candidate_builder = candidate_builder
    self._subnetwork_manager = subnetwork_manager
    self._ensemble_builder = ensemble_builder
    self._ensemblers = ensemblers
    self._summary_maker = summary_maker
    self._replicate_ensemble_in_training = replicate_ensemble_in_training
    self._use_tpu = use_tpu
    super(_IterationBuilder, self).__init__()

  def build_iteration(self,
                      iteration_number,
                      ensemble_candidates,
                      subnetwork_builders,
                      features,
                      mode,
                      labels=None,
                      previous_ensemble_summary=None,
                      previous_ensemble_spec=None,
                      rebuilding=False,
                      params=None):
    """Builds and returns AdaNet iteration t.

    This method uses the generated the candidate subnetworks given the ensemble
    at iteration t-1 and creates graph operations to train them. The returned
    `_Iteration` tracks the training of all candidates to know when the
    iteration is over, and tracks the best candidate's predictions and loss, as
    defined by lowest complexity-regularized loss on the train set.

    Args:
      iteration_number: Integer iteration number.
      ensemble_candidates: Iterable of `adanet.ensemble.Candidate` instances.
      subnetwork_builders: A list of `Builders` for adding ` Subnetworks` to the
        graph. Each subnetwork is then wrapped in a `_Candidate` to train.
      features: Dictionary of `Tensor` objects keyed by feature name.
      mode: Defines whether this is training, evaluation or prediction. See
        `ModeKeys`.
      labels: `Tensor` of labels. Can be `None`.
      previous_ensemble_summary: The `adanet.Summary` for the previous ensemble.
      previous_ensemble_spec: Optional `_EnsembleSpec` for iteration t-1.
      rebuilding: Boolean whether the iteration is being rebuilt only to restore
        the previous best subnetworks and ensembles.
      params: The model_fn params.

    Returns:
      An _Iteration instance.

    Raises:
      ValueError: If subnetwork_builders is empty.
      ValueError: If two subnetworks share the same name.
      ValueError: If two ensembles share the same name.
    """

    tf.logging.info("%s iteration %s",
                    "Rebuilding" if rebuilding else "Building",
                    iteration_number)

    if not subnetwork_builders:
      raise ValueError("Each iteration must have at least one Builder.")

    # TODO: Consider moving builder mode logic to ensemble_builder.py.
    builder_mode = mode
    if rebuilding:
      # Build the subnetworks and ensembles in EVAL mode by default. This way
      # their outputs aren't affected by dropout etc.
      builder_mode = tf.estimator.ModeKeys.EVAL
      if mode == tf.estimator.ModeKeys.PREDICT:
        builder_mode = mode

      # Only replicate in training mode when the user requests it.
      if self._replicate_ensemble_in_training and (
          mode == tf.estimator.ModeKeys.TRAIN):
        builder_mode = mode

    training = mode == tf.estimator.ModeKeys.TRAIN
    skip_summaries = mode == tf.estimator.ModeKeys.PREDICT
    with tf.variable_scope("iteration_{}".format(iteration_number)):
      # Iteration step to use instead of global step.
      iteration_step = tf.get_variable(
          "step",
          shape=[],
          initializer=tf.zeros_initializer(),
          trainable=False,
          dtype=tf.int64)

      # Convert to tensor so that users cannot mutate it.
      iteration_step_tensor = tf.convert_to_tensor(iteration_step)

      seen_builder_names = {}
      candidates = []
      summaries = []
      subnetwork_reports = {}
      previous_ensemble = None

      if previous_ensemble_spec:
        previous_ensemble = previous_ensemble_spec.ensemble
        # Include previous best subnetwork as a candidate so that its
        # predictions are returned until a new candidate outperforms.
        seen_builder_names = {previous_ensemble_spec.name: True}
        previous_best_candidate = self._candidate_builder.build_candidate(
            ensemble_spec=previous_ensemble_spec,
            training=training,
            iteration_step=iteration_step_tensor,
            summary=previous_ensemble_summary,
            is_previous_best=True)
        candidates.append(previous_best_candidate)
        summaries.append(previous_ensemble_summary)

        # Generate subnetwork reports.
        if mode == tf.estimator.ModeKeys.EVAL:
          metrics = {}
          if previous_ensemble_spec.eval_metrics is not None:
            metric_fn, kwargs = previous_ensemble_spec.eval_metrics
            metrics = metric_fn(**kwargs)
          subnetwork_report = subnetwork.Report(
              hparams={},
              attributes={},
              metrics=metrics,
          )
          subnetwork_report.metrics["adanet_loss"] = tf.metrics.mean(
              previous_ensemble_spec.adanet_loss)
          subnetwork_reports["previous_ensemble"] = subnetwork_report

      for subnetwork_builder in subnetwork_builders:
        if subnetwork_builder.name in seen_builder_names:
          raise ValueError("Two subnetworks have the same name '{}'".format(
              subnetwork_builder.name))
        seen_builder_names[subnetwork_builder.name] = True
      subnetwork_specs = []
      for subnetwork_builder in subnetwork_builders:
        subnetwork_name = "t{}_{}".format(iteration_number,
                                          subnetwork_builder.name)
        subnetwork_summary = self._summary_maker(
            namespace="subnetwork",
            scope=subnetwork_name,
            skip_summary=skip_summaries or rebuilding)

        summaries.append(subnetwork_summary)
        subnetwork_spec = self._subnetwork_manager.build_subnetwork_spec(
            name=subnetwork_name,
            subnetwork_builder=subnetwork_builder,
            iteration_step=iteration_step_tensor,
            summary=subnetwork_summary,
            features=features,
            mode=builder_mode,
            labels=labels,
            previous_ensemble=previous_ensemble,
            params=params)
        subnetwork_specs.append(subnetwork_spec)
        # Generate subnetwork reports.
        if mode != tf.estimator.ModeKeys.PREDICT:
          subnetwork_report = subnetwork_builder.build_subnetwork_report()
          if not subnetwork_report:
            subnetwork_report = subnetwork.Report(
                hparams={}, attributes={}, metrics={})
          if subnetwork_spec.eval_metrics is not None:
            metrics_fn, kwargs = subnetwork_spec.eval_metrics
            metrics = metrics_fn(**kwargs)
            for metric_name in sorted(metrics):
              metric = metrics[metric_name]
              subnetwork_report.metrics[metric_name] = metric
          subnetwork_reports[subnetwork_builder.name] = subnetwork_report

      # Create (ensembler_candidate*ensembler) ensembles.
      seen_ensemble_names = {}
      for ensembler in self._ensemblers:
        for ensemble_candidate in ensemble_candidates:
          ensemble_name = "t{}_{}_{}".format(
              iteration_number, ensemble_candidate.name, ensembler.name)
          if ensemble_name in seen_ensemble_names:
            raise ValueError(
                "Two ensembles have the same name '{}'".format(ensemble_name))
          seen_ensemble_names[ensemble_name] = True
          summary = self._summary_maker(
              namespace="ensemble",
              scope=ensemble_name,
              skip_summary=skip_summaries or rebuilding)
          summaries.append(summary)
          ensemble_spec = self._ensemble_builder.build_ensemble_spec(
              name=ensemble_name,
              candidate=ensemble_candidate,
              ensembler=ensembler,
              subnetwork_specs=subnetwork_specs,
              summary=summary,
              features=features,
              mode=builder_mode,
              iteration_step=iteration_step_tensor,
              iteration_number=iteration_number,
              labels=labels,
              previous_ensemble_spec=previous_ensemble_spec,
              params=params)
          candidate = self._candidate_builder.build_candidate(
              ensemble_spec=ensemble_spec,
              training=training,
              iteration_step=iteration_step_tensor,
              summary=summary)
          candidates.append(candidate)
          # TODO: Move adanet_loss from subnetwork report to a new
          # ensemble report, since the adanet_loss is associated with an
          # ensemble, and only when using a ComplexityRegularizedEnsemblers.
          # Keep adanet_loss in subnetwork report for backwards compatibility.
          if len(ensemble_candidates) != len(subnetwork_builders):
            continue
          if len(ensemble_candidate.subnetwork_builders) > 1:
            continue
          if mode == tf.estimator.ModeKeys.PREDICT:
            continue
          builder_name = ensemble_candidate.subnetwork_builders[0].name
          subnetwork_reports[builder_name].metrics[
              "adanet_loss"] = tf.metrics.mean(ensemble_spec.adanet_loss)

      # Dynamically select the outputs of best candidate.
      best_candidate_index = self._best_candidate_index(candidates)
      best_predictions = self._best_predictions(candidates,
                                                best_candidate_index)
      best_loss = self._best_loss(candidates, best_candidate_index, mode)
      best_eval_metrics = self._create_best_eval_metrics_tuple(
          candidates, subnetwork_specs, best_candidate_index, mode, params)
      best_export_outputs = self._best_export_outputs(
          candidates, best_candidate_index, mode, best_predictions)
      # Hooks on TPU cannot depend on any graph `Tensors`. Instead the value of
      # `is_over` is stored in a `Variable` that can later be retrieved from
      # inside a training hook.
      is_over_var_template = tf.make_template("is_over_var_template",
                                              _is_over_var)

      training_chief_hooks, training_hooks = (), ()
      for subnetwork_spec in subnetwork_specs:
        if not subnetwork_spec.train_op:
          continue
        training_chief_hooks += subnetwork_spec.train_op.chief_hooks or ()
        training_hooks += subnetwork_spec.train_op.hooks or ()
      for candidate in candidates:
        spec = candidate.ensemble_spec
        if not spec.train_op:
          continue
        training_chief_hooks += spec.train_op.chief_hooks or ()
        training_hooks += spec.train_op.hooks or ()
      summary = self._summary_maker(
          namespace=None, scope=None, skip_summary=skip_summaries or rebuilding)
      summaries.append(summary)
      with summary.current_scope():
        summary.scalar("iteration/adanet/iteration", iteration_number)
        summary.scalar("iteration_step/adanet/iteration_step",
                       iteration_step_tensor)
        if best_loss is not None:
          summary.scalar("loss", best_loss)
      eval_metric_ops = None
      if best_eval_metrics is not None:
        metric_fn, kwargs = best_eval_metrics
        eval_metric_ops = metric_fn(**kwargs)
      train_op = self._create_train_op(subnetwork_specs, candidates, mode,
                                       iteration_step, is_over_var_template)
      if self._use_tpu:
        estimator_spec = tf.contrib.tpu.TPUEstimatorSpec(
            mode=mode,
            predictions=best_predictions,
            loss=best_loss,
            train_op=train_op,
            eval_metrics=best_eval_metrics,
            export_outputs=best_export_outputs,
            training_hooks=training_hooks)
      else:
        eval_metric_ops = None
        if best_eval_metrics is not None:
          metric_fn, kwargs = best_eval_metrics
          eval_metric_ops = metric_fn(**kwargs)
        estimator_spec = tf.estimator.EstimatorSpec(
            mode=mode,
            predictions=best_predictions,
            loss=best_loss,
            train_op=train_op,
            eval_metric_ops=eval_metric_ops,
            export_outputs=best_export_outputs,
            training_chief_hooks=training_chief_hooks,
            training_hooks=training_hooks)

      return _Iteration(
          number=iteration_number,
          candidates=candidates,
          subnetwork_specs=subnetwork_specs,
          estimator_spec=estimator_spec,
          best_candidate_index=best_candidate_index,
          summaries=summaries,
          is_over_fn=is_over_var_template,
          subnetwork_reports=subnetwork_reports,
          step=iteration_step_tensor)

  def _assign_is_over(self, candidates, is_over_var_template):
    """Assigns whether the iteration is over to the is_over_var.

    The iteration is over once all candidates are done training.

    Workers can only assign `is_over_var` to `True` when they think the
    iteration is over, so that `is_over` cannot be undone in distributed
    training. Effectively, the fastest worker will always determine when
    training is over.

    Args:
      candidates: List of `_Candidate` instances to train.
      is_over_var_template: A fn()->tf.Variable which returns the is_over_var.

    Returns:
      An op which assigns whether the iteration is over to the is_over_var.
    """

    with tf.variable_scope("is_over"):
      is_over = True
      for candidate in candidates:
        is_over = tf.logical_and(is_over, tf.logical_not(candidate.is_training))
      is_over_var = is_over_var_template()
      return tf.cond(
          is_over, lambda: tf.assign(is_over_var, True, name="assign_is_over"),
          lambda: tf.no_op("noassign_is_over"))

  def _create_train_op(self, subnetwork_specs, candidates, mode, step,
                       is_over_var_template):
    """Returns the train op for this set of candidates.

    This train op combines the train ops from all the candidates into a single
    train op. Additionally, it is responsible for incrementing the global step.

    The train op is only non-None during the `TRAIN` mode.

    Args:
      subnetwork_specs: List of `_SubnetworkSpec` instances for this iteration.
      candidates: List of `_Candidate` instances to train.
      mode: Defines whether this is training, evaluation or inference. The train
        op is only non-None during `TRAIN`. See `ModeKeys`.
      step: Integer `Variable` for the current step of the iteration, as opposed
        to the global step.
      is_over_var_template: A fn()->tf.Variable which returns the is_over_var.

    Returns:
      A `Tensor` train op.
    """

    if mode != tf.estimator.ModeKeys.TRAIN:
      return tf.no_op()
    with tf.variable_scope("train_op"):
      train_ops = []
      for subnetwork_spec in subnetwork_specs:
        if subnetwork_spec.train_op is not None:
          train_ops.append(subnetwork_spec.train_op.train_op)
      for candidate in candidates:
        if candidate.ensemble_spec.train_op is not None:
          # The train op of a previous ensemble is None even during `TRAIN`.
          train_ops.append(candidate.ensemble_spec.train_op.train_op)
      train_ops.append(self._assign_is_over(candidates, is_over_var_template))
      with tf.control_dependencies(train_ops):
        # AdaNet is responsible for incrementing the global step, not the
        # candidates it trains. Incrementing the global step and iteration step
        # is the final action performed in the train op.
        return tf.group(
            tf.assign_add(tf.train.get_global_step(), 1),
            tf.assign_add(step, 1),
        )

  def _create_best_eval_metrics_tuple(self, candidates, subnetwork_specs,
                                      best_candidate_index, mode, params):
    """Returns (metric_fn, tensors) which computes the best ensemble's metrics.

    Specifically, when metric_fn(tensors) is called, it separates the metric ops
    by metric name. All candidates are not required to have the same metrics.
    When they all share a given metric, an additional metric is added which
    represents that of the best candidate.

    Args:
      candidates: List of `_Candidate` instances to choose from.
      subnetwork_specs: List of `_SubnetworkSpec` instances for this iteration.
      best_candidate_index: `Tensor` index of the best candidate in the list.
      mode: Defines whether this is training, evaluation or inference. Eval
        metrics are only defined during evaluation. See `ModeKeys`.
      params: The params passed to model_fn.

    Returns:
      Dict of metric results keyed by name. The values of the dict are the
      results of calling a metric function.
    """

    if mode != tf.estimator.ModeKeys.EVAL:
      return None

    metric_fns, tensors = self._collate_metric_fns_and_tensors(
        candidates, subnetwork_specs)
    # All tensors outfed from the TPU must be batch-major.
    batch_size = params.get("batch_size", 1) if params else 1
    tensors["best_candidate_index"] = tf.tile([best_candidate_index],
                                              [batch_size])
    tensors = dict_utils.flatten_dict(tensors)

    def _best_eval_metrics_fn(**kwargs):
      """Returns the best eval metrics."""

      with tf.variable_scope("best_eval_metrics"):
        subnetwork_metric_fns = {
            k: metric_fns[k] for k in metric_fns if k.startswith("subnetwork_")
        }
        subnetwork_tensors = dict_utils.unflatten_dict(
            kwargs, subnetwork_metric_fns.keys())
        subnetwork_metric_ops = self._group_metric_ops(subnetwork_metric_fns,
                                                       subnetwork_tensors)
        ensemble_metric_fns = {
            k: metric_fns[k] for k in metric_fns if k.startswith("ensemble_")
        }
        ensemble_tensors = dict_utils.unflatten_dict(kwargs,
                                                     ensemble_metric_fns.keys())
        grouped_metrics = self._group_metric_ops(ensemble_metric_fns,
                                                 ensemble_tensors)

        eval_metric_ops = {}
        for metric_name in sorted(grouped_metrics):
          metric_ops = grouped_metrics[metric_name]
          if len(metric_ops) != len(candidates):
            continue
          if metric_name == "loss":
            continue

          best_candidate_index = kwargs["best_candidate_index"]
          values, ops = list(six.moves.zip(*metric_ops))
          idx, idx_update_op = tf.metrics.mean(best_candidate_index)
          best_value = tf.stack(values)[tf.cast(idx, tf.int32)]
          # All tensors in this function have been outfed from the TPU, so we
          # must update them manually, otherwise the TPU will hang indefinetly
          # for the value of idx to update.
          ops = list(ops)
          ops.append(idx_update_op)
          # Bundle subnetwork eval metric ops and ensemble "loss"" ops (which
          # is a restricted Estimator keyword) into other metric ops so that
          # they are computed.
          ensemble_loss_ops = grouped_metrics.get("loss", tf.no_op())
          all_ops = tf.group(ops, ensemble_loss_ops, subnetwork_metric_ops)
          eval_metric_ops[metric_name] = (best_value, all_ops)

        # tf.estimator.Estimator does not allow a "loss" key to be present in
        # its eval_metrics.
        assert "loss" not in eval_metric_ops
        return eval_metric_ops

    return _best_eval_metrics_fn, tensors

  def _collate_metric_fns_and_tensors(self, candidates, subnetwork_specs):
    """Collates all candidates' metric_fns and tensors from their eval_metrics.

    The fns and tensors are keyed by the index of the candidate in the list.

    Args:
      candidates: List of `_Candidate` instances.
      subnetwork_specs: List of `_SubnetworkSpec` instances for this iteration.

    Returns:
      The dicts of functions and tensors keyed by candidate index.
    """
    fns = {}
    tensors = {}
    for i, subnetwork_spec in enumerate(subnetwork_specs):
      if not subnetwork_spec.eval_metrics:
        continue
      metric_fn, metric_tensors = subnetwork_spec.eval_metrics
      key = "subnetwork_{}".format(i)
      fns[key] = metric_fn
      tensors[key] = metric_tensors
    for i, candidate in enumerate(candidates):
      ensemble_spec = candidate.ensemble_spec
      if not ensemble_spec.eval_metrics:
        continue
      metric_fn, metric_tensors = ensemble_spec.eval_metrics
      key = "ensemble_{}".format(i)
      fns[key] = metric_fn
      tensors[key] = metric_tensors
    return fns, tensors

  def _group_metric_ops(self, metric_fns, tensors):
    """Runs the metric_fns and groups the returned metric ops by name.

    Tensors will be passed as params to metric_fns which have the same key. The
    dicts of eval metrics returned by metric_fns are then reduced by key.

    Args:
      metric_fns: A dictionary of fn(tensors)->dict(metric_name, metric_ops).
      tensors: A dictionary of tensors to pass to metric_fns.

    Returns:
      The metric ops grouped by name.
    """

    grouped_metrics = {}
    for key in sorted(metric_fns):
      fn = metric_fns[key]
      args = tensors[key]
      eval_metric_ops = fn(**args)
      for metric_name in sorted(eval_metric_ops):
        metric_op = eval_metric_ops[metric_name]
        if metric_name not in grouped_metrics:
          grouped_metrics[metric_name] = []
        grouped_metrics[metric_name].append(metric_op)
    return grouped_metrics

  def _best_candidate_index(self, candidates):
    """Returns the index of the best candidate in the list.

    The best candidate is the one with the smallest AdaNet loss.

    Args:
      candidates: List of `_Candidate` instances to choose from.

    Returns:
      An integer `Tensor` representing the index of the best candidate.
    """

    with tf.variable_scope("best_candidate_index"):
      if len(candidates) == 1:
        return tf.constant(0)
      adanet_losses = [candidate.adanet_loss for candidate in candidates]
      return tf.argmin(adanet_losses, axis=0)

  def _best_predictions(self, candidates, best_candidate_index):
    """Returns the best predictions from a set of candidates.

    Args:
      candidates: List of `_Candidate` instances to compare.
      best_candidate_index: `Tensor` index of the best candidate in the list.

    Returns:
      A `Tensor` or dictionary of `Tensor`s representing the best candidate's
      predictions (depending on what the subnetworks return).
    """

    if len(candidates) == 1:
      return candidates[0].ensemble_spec.predictions

    with tf.variable_scope("best_predictions"):
      predictions = None
      for candidate in candidates:
        ensemble_spec = candidate.ensemble_spec
        if isinstance(ensemble_spec.predictions, dict):
          if not predictions:
            predictions = {}
          for key in sorted(ensemble_spec.predictions):
            tensor = ensemble_spec.predictions[key]
            if key in predictions:
              predictions[key].append(tensor)
            else:
              predictions[key] = [tensor]
        else:
          if not predictions:
            predictions = []
          predictions.append(ensemble_spec.predictions)

      if isinstance(predictions, dict):
        best_predictions = {}
        for key in sorted(predictions):
          tensor_list = predictions[key]
          best_predictions[key] = tf.stack(tensor_list)[best_candidate_index]
      else:
        best_predictions = tf.stack(predictions)[best_candidate_index]
      return best_predictions

  def _best_loss(self, candidates, best_candidate_index, mode):
    """Returns the best loss from a set of candidates.

    Args:
      candidates: List of `_Candidate` instances to compare.
      best_candidate_index: `Tensor` index of the best candidate in the list.
      mode: Defines whether this is training, evaluation or inference. Loss is
        always None during inference. See `ModeKeys`.

    Returns:
      Float `Tensor` of the best candidate's loss.
    """

    if mode == tf.estimator.ModeKeys.PREDICT:
      return None
    if len(candidates) == 1:
      return candidates[0].ensemble_spec.loss
    with tf.variable_scope("best_loss"):
      losses = [c.ensemble_spec.loss for c in candidates]
      loss = tf.slice(tf.stack(losses), [best_candidate_index], [1])
      return tf.reshape(loss, [])

  def _best_export_outputs(self, candidates, best_candidate_index, mode,
                           best_predictions):
    """Returns the best `SavedModel` export outputs from a set of candidates.

    Assumes that all candidate ensembles have identical export output keys and
    `ExportOutput` types.

    Args:
      candidates: List of `_Candidate` instances to compare.
      best_candidate_index: `Tensor` index of the best candidate in the list.
      mode: Defines whether this is training, evaluation or inference. Export
        outputs are always None during training and evaluation. See `ModeKeys`.
      best_predictions: A `Tensor` or dictionary of `Tensor`s representing the
        best candidate's predictions (depending on what the subnetworks return).

    Returns:
      A `Tensor` dictionary representing the best candidate's export outputs.

    Raises:
      TypeError: If the `ExportOutput` type is not supported.
    """

    if mode != tf.estimator.ModeKeys.PREDICT:
      return None
    if len(candidates) == 1:
      return candidates[0].ensemble_spec.export_outputs
    with tf.variable_scope("best_export_outputs"):
      # Group tensors by export output key and ExportOutput type.
      export_outputs = {}
      for candidate in candidates:
        ensemble_spec = candidate.ensemble_spec
        for key in sorted(ensemble_spec.export_outputs):
          export_output = ensemble_spec.export_outputs[key]
          if isinstance(export_output,
                        tf.estimator.export.ClassificationOutput):
            if key not in export_outputs:
              export_outputs[key] = ([], [])
            if export_output.scores is not None:
              export_outputs[key][0].append(export_output.scores)
            if export_output.classes is not None:
              export_outputs[key][1].append(export_output.classes)
          elif isinstance(export_output, tf.estimator.export.RegressionOutput):
            if key not in export_outputs:
              export_outputs[key] = []
            export_outputs[key].append(export_output.value)
          elif isinstance(export_output, tf.estimator.export.PredictOutput):
            # Use self._best_predictions() below to get prediction output.
            continue
          else:
            raise TypeError(
                "Values in export_outputs must be ClassificationOutput, "
                "RegressionOutput, or PredictOutput objects. Given: {}".format(
                    export_output))

      # Stack tensor lists into correct ExportOutput type, outputting the
      # correct values based on the best candidate index.
      best_export_outputs = {}
      for key in sorted(candidates[0].ensemble_spec.export_outputs):
        export_output = candidates[0].ensemble_spec.export_outputs[key]
        if isinstance(export_output, tf.estimator.export.ClassificationOutput):
          scores, classes = None, None
          if export_outputs[key][0]:
            scores = tf.stack(export_outputs[key][0])[best_candidate_index]
          if export_outputs[key][1]:
            classes = tf.stack(export_outputs[key][1])[best_candidate_index]
          output = tf.estimator.export.ClassificationOutput(
              scores=scores, classes=classes)
        elif isinstance(export_output, tf.estimator.export.RegressionOutput):
          value = tf.stack(export_outputs[key])[best_candidate_index]
          output = tf.estimator.export.RegressionOutput(value)
        else:
          output = tf.estimator.export.PredictOutput(best_predictions)
        best_export_outputs[key] = output
      return best_export_outputs
