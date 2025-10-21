import sys
from dataclasses import dataclass
from typing import Dict, List

import numpy as np
import time

from cody.explainer.baseline.irand import IRandExplainer
from cody.implementations.connector import TGNNWrapper
from cody.constants import CUR_IT_MIN_EVENT_MEM_LBL, EXPLAINED_EVENT_MEMORY_LABEL, COL_ID
from cody.explainer.base import Explainer, CounterFactualExample, TreeNode
from cody.explainer.greedy import GreedyCFExplainer, GreedyTreeNode
from cody.selection import SelectionPolicy, LocalEventImpactSelectionPolicy
from cody.explainer.cody import CoDy, CoDyTreeNode
from cody.explainer.cody import find_best_non_counterfactual_example as find_best_non_cf_example
from time_to_explain.utils.utils import ProgressBar

EVALUATION_STATE_CACHE = {}


@dataclass
class PredictionResult:
    prediction_time_ns: int
    prediction: float


@dataclass
class EvaluationCounterFactualExample(CounterFactualExample):
    timings: Dict
    statistics: Dict

    def to_dict(self) -> Dict:
        results = {
            'explained_event_id': self.explained_event_id,
            'original_prediction': self.original_prediction,
            'counterfactual_prediction': self.counterfactual_prediction,
            'achieves_counterfactual_explanation': self.achieves_counterfactual_explanation,
            'cf_example_event_ids': self.event_ids,
            'cf_example_absolute_importances': self.get_absolute_importances(),
            'cf_example_raw_importances': self.event_importances
        }
        results.update(self.statistics)
        results.update(self.timings)
        return results


class EvaluationExplainer(Explainer):
    explanation_results_list: List[EvaluationCounterFactualExample]

    def __init__(self, tgnn_wrapper: TGNNWrapper, selection_policy: str = 'recent', candidates_size: int = 75,
                 sample_size: int = 10, verbose: bool = False, approximate_predictions: bool = True):
        super().__init__(tgnn_wrapper, selection_policy, candidates_size, sample_size, verbose,
                         approximate_predictions)
        self.explanation_results_list = []

    def initialize_explanation_evaluation(self, explained_event_id: int, original_prediction: float) -> SelectionPolicy:
        subgraph = self.subgraph_generator.get_fixed_size_k_hop_temporal_subgraph(num_hops=self.num_hops,
                                                                                  base_event_id=explained_event_id,
                                                                                  size=self.candidates_size)
        self.tgnn.set_evaluation_mode(True)
        return self._create_sampler(subgraph, explained_event_id, original_prediction=original_prediction)

    def evaluate_explanation(self, explained_event_id: int, original_prediction: float) -> (
            EvaluationCounterFactualExample):
        """
        Explain the provided event
        @param explained_event_id: Event id to explain
        @param original_prediction: Original prediction for the event
        @return: The counterfactual explanation
        """
        raise NotImplementedError


class EvaluationGreedyCFExplainer(GreedyCFExplainer, EvaluationExplainer):

    def __init__(self, tgnn_wrapper: TGNNWrapper, selection_policy: str = 'recent', sample_size: int = 10,
                 candidates_size: int = 64, verbose: bool = False, approximate_predictions: bool = True):
        super(GreedyCFExplainer, self).__init__(tgnn_wrapper=tgnn_wrapper, selection_policy=selection_policy,
                                                sample_size=sample_size, candidates_size=candidates_size,
                                                verbose=verbose, approximate_predictions=approximate_predictions)
        super(EvaluationExplainer, self).__init__(tgnn_wrapper=tgnn_wrapper, selection_policy=selection_policy,
                                                  candidates_size=candidates_size, sample_size=sample_size,
                                                  verbose=verbose, approximate_predictions=approximate_predictions)
        self.last_min_id = 0

    def create_child_node(self, node_to_expand: TreeNode, memory_label: str, explained_event_id: int,
                          candidate_event_id: int, sampled_edge_ids):
        child_hash = f'{explained_event_id}-{node_to_expand.hash()}-{candidate_event_id}'
        exp_cache_save_time = 0
        if child_hash in EVALUATION_STATE_CACHE.keys():
            result = EVALUATION_STATE_CACHE[child_hash]
            oracle_call_duration = result.prediction_time_ns
            exp_cache_save_time = result.prediction_time_ns
            prediction = result.prediction
        else:
            oracle_call_start = time.time_ns()
            prediction = self.calculate_subgraph_prediction(candidate_events=sampled_edge_ids,
                                                            cf_example_events=node_to_expand.get_parent_ids() +
                                                                              [node_to_expand.edge_id],
                                                            explained_event_id=explained_event_id,
                                                            candidate_event_id=candidate_event_id,
                                                            original_prediction=node_to_expand.original_prediction,
                                                            memory_label=memory_label)
            oracle_call_duration = time.time_ns() - oracle_call_start
            EVALUATION_STATE_CACHE[child_hash] = PredictionResult(oracle_call_duration, prediction)
        child_node = GreedyTreeNode(candidate_event_id, parent=node_to_expand,
                                    original_prediction=node_to_expand.original_prediction, prediction=prediction)
        node_to_expand.children.append(child_node)
        return child_node, oracle_call_duration, exp_cache_save_time

    def evaluate_explanation(self, explained_event_id: int,
                             original_prediction: float) -> EvaluationCounterFactualExample:
        if original_prediction is None:
            original_prediction, sampler = self.initialize_explanation(explained_event_id)
        else:
            sampler = self.initialize_explanation_evaluation(explained_event_id, original_prediction)
        timings = {}
        statistics = {}
        oracle_calls = 0
        oracle_call_time = 0
        cache_saved_oracle_call_time = 0
        start_time = time.time_ns()
        min_event_id = sampler.subgraph[COL_ID].min() - 1
        root_node = GreedyTreeNode(explained_event_id, None, original_prediction=original_prediction,
                                   prediction=original_prediction)
        max_depth = sys.maxsize
        best_cf_example = None
        best_non_cf_example = root_node
        skip_search = False

        if type(sampler) is LocalEventImpactSelectionPolicy:
            for child_id in sampler.rank_subgraph(base_event_id=explained_event_id, excluded_events=np.array([])):
                child_node, oc_duration, saved_time = self.create_child_node(node_to_expand=root_node,
                                                                             memory_label=EXPLAINED_EVENT_MEMORY_LABEL,
                                                                             explained_event_id=explained_event_id,
                                                                             candidate_event_id=child_id,
                                                                             sampled_edge_ids=
                                                                             sampler.subgraph[COL_ID].to_numpy())
                oracle_call_time += oc_duration
                cache_saved_oracle_call_time += saved_time
                oracle_calls += 1
                if child_node.is_counterfactual:
                    if best_cf_example is None:
                        best_cf_example = child_node
                    elif best_cf_example.exploitation_score < child_node.exploitation_score:
                        best_cf_example = child_node
                    if self.verbose:
                        self.logger.info(f'Found counterfactual explanation: ' + str(child_node.to_cf_example()))
                sampler.set_event_weight(child_node.edge_id, child_node.exploitation_score)
            if best_cf_example is not None:
                skip_search = True
            root_node.expanded = True

        i = 0
        init_end_time = time.time_ns()
        timings['init_duration'] = init_end_time - start_time
        while not skip_search:
            node_to_expand = root_node.select_next_leaf(max_depth)
            if node_to_expand is None or (node_to_expand == root_node and root_node.expanded):
                break  # No more nodes can be selected -> conclude search without a cf-example
            best_non_cf_example = node_to_expand
            if self.verbose:
                self.logger.info(f'Iteration {i} selected event {node_to_expand.edge_id} with prediction '
                                 f'{node_to_expand.prediction}. '
                                 f'CF-example events: {node_to_expand.hash()}')
            sampled_edge_ids = sampler.sample(explained_event_id,
                                              excluded_events=np.array(node_to_expand.get_parent_ids()),
                                              size=self.sample_size)
            self.tgnn.initialize(min_event_id, show_progress=False,
                                 memory_label=EXPLAINED_EVENT_MEMORY_LABEL)
            for candidate_event_id in sampled_edge_ids:
                child_node, oracle_call_duration, exp_cache_save_time = (
                    self.create_child_node(node_to_expand, memory_label=CUR_IT_MIN_EVENT_MEM_LBL,
                                           explained_event_id=explained_event_id,
                                           candidate_event_id=candidate_event_id,
                                           sampled_edge_ids=sampled_edge_ids))
                oracle_call_time += oracle_call_duration
                oracle_calls += 1
                cache_saved_oracle_call_time += exp_cache_save_time
                if child_node.is_counterfactual:
                    if best_cf_example is None:
                        best_cf_example = child_node
                    elif best_cf_example.exploitation_score < child_node.exploitation_score:
                        best_cf_example = child_node
                    if self.verbose:
                        self.logger.info(f'Found counterfactual explanation: ' + str(child_node.to_cf_example()))
            self.tgnn.remove_memory_backup(CUR_IT_MIN_EVENT_MEM_LBL)
            node_to_expand.expanded = True
            if best_cf_example is not None:
                break
            i += 1

        best_example = best_cf_example
        if best_example is None:
            best_example = best_non_cf_example
        self.tgnn.remove_memory_backup(EXPLAINED_EVENT_MEMORY_LABEL)
        self.tgnn.reset_model()
        end_time = time.time_ns()
        timings['oracle_call_duration'] = oracle_call_time
        timings['explanation_duration'] = end_time - start_time - oracle_call_time + cache_saved_oracle_call_time
        timings['total_duration'] = end_time - start_time + cache_saved_oracle_call_time
        statistics['oracle_calls'] = oracle_calls
        statistics['candidate_size'] = len(sampler.subgraph)
        statistics['candidates'] = sampler.subgraph[COL_ID].to_numpy()
        result_cf_example = best_example.to_cf_example()
        cf_example = EvaluationCounterFactualExample(explained_event_id=explained_event_id,
                                                     original_prediction=original_prediction,
                                                     counterfactual_prediction=
                                                     result_cf_example.counterfactual_prediction,
                                                     achieves_counterfactual_explanation=
                                                     result_cf_example.achieves_counterfactual_explanation,
                                                     event_ids=result_cf_example.event_ids,
                                                     event_importances=result_cf_example.event_importances,
                                                     timings=timings,
                                                     statistics=statistics)
        if self.verbose:
            self.logger.info(f'Final explanation result: {str(cf_example)}\n')
        return cf_example


class EvaluationCoDy(CoDy, EvaluationExplainer):

    def __init__(self, tgnn_wrapper: TGNNWrapper, selection_policy: str = 'recent', max_steps: int = 300,
                 candidates_size: int = 64, verbose: bool = False, approximate_predictions: bool = True,
                 alpha: float = 2/3):
        CoDy.__init__(self, tgnn_wrapper=tgnn_wrapper, selection_policy=selection_policy,
                      candidates_size=candidates_size, verbose=verbose, max_steps=max_steps,
                      approximate_predictions=approximate_predictions, alpha=alpha)
        EvaluationExplainer.__init__(self, tgnn_wrapper=tgnn_wrapper, selection_policy=selection_policy,
                                     candidates_size=candidates_size, sample_size=candidates_size, verbose=verbose,
                                     approximate_predictions=approximate_predictions)
        self.last_min_id = -1

    def _get_evaluation_subgraph_prediction(self, candidate_events: np.ndarray, node_to_expand: CoDyTreeNode,
                                            explained_event_id: int,
                                            memory_label: str = EXPLAINED_EVENT_MEMORY_LABEL) -> (float, int, int):
        full_hash = f'{explained_event_id}-{node_to_expand.hash()}'
        if full_hash in EVALUATION_STATE_CACHE.keys():
            result = EVALUATION_STATE_CACHE[full_hash]
            return result.prediction, result.prediction_time_ns, result.prediction_time_ns
        else:
            oracle_call_start_time = time.time_ns()
            prediction = self.calculate_subgraph_prediction(candidate_events=candidate_events,
                                                            cf_example_events=node_to_expand.get_parent_ids(),
                                                            explained_event_id=explained_event_id,
                                                            candidate_event_id=node_to_expand.edge_id,
                                                            original_prediction=node_to_expand.original_prediction,
                                                            memory_label=memory_label)
            oracle_call_time = time.time_ns() - oracle_call_start_time
            EVALUATION_STATE_CACHE[full_hash] = PredictionResult(oracle_call_time, prediction)
            return prediction, oracle_call_time, 0

    def _run_node_expansion(self, explained_edge_id: int, node_to_expand: CoDyTreeNode, sampler: SelectionPolicy):
        prediction, oracle_call_time, cache_save_time = (
            self._get_evaluation_subgraph_prediction(candidate_events=sampler.subgraph[COL_ID].to_numpy(),
                                                     node_to_expand=node_to_expand,
                                                     explained_event_id=explained_edge_id,
                                                     memory_label=EXPLAINED_EVENT_MEMORY_LABEL))
        self._expand_node(explained_edge_id, node_to_expand, prediction, sampler)
        return oracle_call_time, cache_save_time

    def evaluate_explanation(self, explained_event_id: int, original_prediction: float) -> (
            EvaluationCounterFactualExample):
        if original_prediction is None:
            original_prediction, sampler = self.initialize_explanation(explained_event_id)
        else:
            sampler = self.initialize_explanation_evaluation(explained_event_id, original_prediction)
        timings = {}
        statistics = {}
        oracle_calls = 0
        oracle_call_time = 0
        cache_saved_oracle_call_time = 0
        encountered_cf_examples = 0
        start_time = time.time_ns()

        best_cf_example = None
        best_cf_example_step = 0
        first_example_step = self.max_steps + 1
        step = 0
        skip_search = False
        max_depth = sys.maxsize
        root_node = CoDyTreeNode(explained_event_id, parent=None, sampling_rank=0,
                                 original_prediction=original_prediction, alpha=self.alpha)
        self._expand_node(explained_event_id, root_node, original_prediction, sampler)

        if type(sampler) is LocalEventImpactSelectionPolicy:
            for child in root_node.children:
                # Expand all children
                exp_oracle_call_time, exp_cache_save_time = self._run_node_expansion(explained_event_id, child, sampler)
                oracle_call_time += exp_oracle_call_time
                cache_saved_oracle_call_time += exp_cache_save_time
                oracle_calls += 1
                if child.is_counterfactual:
                    first_example_step = 0
                    if best_cf_example is None:
                        best_cf_example = child
                    elif best_cf_example.exploitation_score < child.exploitation_score:
                        best_cf_example = child
                    if self.verbose:
                        self.logger.info(f'Found counterfactual explanation: '
                                         + str(child.to_cf_example()))
                sampler.set_event_weight(child.edge_id, child.exploitation_score)
            if best_cf_example is not None:
                skip_search = True
            step += 1
        init_end_time = time.time_ns()
        timings['init_duration'] = init_end_time - start_time
        progress_bar = ProgressBar(self.max_steps)
        while step <= self.max_steps and not skip_search:
            progress_bar.next()
            node_to_expand = None
            while node_to_expand is None:
                node_to_expand = root_node.select_next_leaf(max_depth)
                if node_to_expand.depth > max_depth:
                    # Should not happen. TODO: Check if it is save to remove this if-condition
                    node_to_expand.expansion_backpropagation()
                    node_to_expand = None
                    continue
                if node_to_expand == root_node and root_node.expanded:
                    break  # No nodes are selectable, meaning that we can conclude the search
                if node_to_expand.hash() in self.known_states.keys():
                    # Already encountered this combination -> select new combination of events instead
                    self._expand_node(explained_event_id, node_to_expand, self.known_states[node_to_expand.hash()],
                                      sampler)
                    node_to_expand = None
            if node_to_expand == root_node and root_node.expanded:
                if self.verbose:
                    self.logger.info('Search Tree is fully expanded. Concluding search.')
                break  # No nodes are selectable, meaning that we can conclude the search
            exp_oracle_call_time, exp_cache_save_time = self._run_node_expansion(explained_event_id, node_to_expand,
                                                                                 sampler)
            if self.verbose:
                self.logger.info(f'[{step}/{self.max_steps}] Selected node {node_to_expand.edge_id} '
                                 f'at depth {node_to_expand.depth}, '
                                 f'prediction: {node_to_expand.prediction}, '
                                 f'exploitation score: {node_to_expand.exploitation_score}, hash: '
                                 f'{node_to_expand.hash()}')
            oracle_call_time += exp_oracle_call_time
            cache_saved_oracle_call_time += exp_cache_save_time
            oracle_calls += 1
            if node_to_expand.is_counterfactual:
                if best_cf_example is None or best_cf_example.depth > node_to_expand.depth:
                    if best_cf_example is None:
                        first_example_step = step
                    best_cf_example = node_to_expand
                    best_cf_example_step = step
                    encountered_cf_examples += 1
                elif (best_cf_example.depth == node_to_expand.depth and
                      best_cf_example.exploitation_score < node_to_expand.exploitation_score):
                    if best_cf_example is None:
                        first_example_step = step
                    best_cf_example = node_to_expand
                    best_cf_example_step = step
                    encountered_cf_examples += 1
                max_depth = best_cf_example.depth
                if self.verbose:
                    self.logger.info(f'Found counterfactual explanation: '
                                     + str(node_to_expand.to_cf_example()))
            step += 1
        if best_cf_example is None:
            best_cf_example = find_best_non_cf_example(root_node)
            best_cf_example_step = step
        self.tgnn.remove_memory_backup(EXPLAINED_EVENT_MEMORY_LABEL)
        self.tgnn.reset_model()
        self.known_states = {}
        progress_bar.close()
        end_time = time.time_ns()
        timings['oracle_call_duration'] = oracle_call_time
        timings['explanation_duration'] = end_time - start_time - oracle_call_time + cache_saved_oracle_call_time
        timings['total_duration'] = end_time - start_time + cache_saved_oracle_call_time
        statistics['oracle_calls'] = oracle_calls
        statistics['candidate_size'] = len(sampler.subgraph)
        statistics['candidates'] = sampler.subgraph[COL_ID].to_numpy()
        statistics['cf_example_step'] = best_cf_example_step
        statistics['first_example_step'] = first_example_step
        statistics['encountered_cf_examples'] = encountered_cf_examples
        cf_ex = best_cf_example.to_cf_example()
        eval_cf_example = EvaluationCounterFactualExample(explained_event_id=explained_event_id,
                                                          original_prediction=original_prediction,
                                                          counterfactual_prediction=cf_ex.counterfactual_prediction,
                                                          achieves_counterfactual_explanation=
                                                          cf_ex.achieves_counterfactual_explanation,
                                                          event_ids=cf_ex.event_ids,
                                                          event_importances=cf_ex.event_importances,
                                                          timings=timings,
                                                          statistics=statistics)
        return eval_cf_example


class EvaluationIRandExplainer(IRandExplainer, EvaluationExplainer):

    def evaluate_explanation(self, explained_event_id: int, original_prediction: float) -> (
            EvaluationCounterFactualExample):
        subgraph = self.subgraph_generator.get_fixed_size_k_hop_temporal_subgraph(num_hops=self.num_hops,
                                                                                  base_event_id=explained_event_id,
                                                                                  size=self.candidates_size)
        min_event_id = subgraph[COL_ID].min() - 1  # One less since we do not want to simulate the minimal event

        self.tgnn.set_evaluation_mode(True)
        self.tgnn.reset_model()
        if original_prediction is None:
            original_prediction = self.calculate_original_score(explained_event_id, min_event_id)

        best_cf_example = EvaluationCounterFactualExample(explained_event_id, original_prediction, original_prediction,
                                                          False, np.array([]), np.array([None]), {}, {})

        for i in range(1, min(self.max_iterations, len(subgraph))):
            self.tgnn.reset_model()
            subgraph_sample = subgraph[COL_ID].sample(n=i, replace=False).to_list()
            prediction = self.calculate_subgraph_prediction(candidate_events=subgraph[COL_ID].to_numpy(),
                                               cf_example_events=subgraph_sample[:i - 1],
                                               explained_event_id=explained_event_id,
                                               candidate_event_id=subgraph_sample[i - 1],
                                               original_prediction=original_prediction,
                                               memory_label=EXPLAINED_EVENT_MEMORY_LABEL)
            if prediction * original_prediction < 0:
                print("Identified counterfactual of size " + str(i))
                best_cf_example = EvaluationCounterFactualExample(explained_event_id, original_prediction, prediction,
                                                                  True, subgraph_sample, np.array([None] * i), {}, {})
                break

            # Fallback to best identified explanation that is not counterfactual
            if original_prediction > 0:
                if ((original_prediction - best_cf_example.counterfactual_prediction) <
                        (original_prediction - prediction)):
                    best_cf_example = EvaluationCounterFactualExample(explained_event_id, original_prediction,
                                                                      prediction, False, subgraph_sample,
                                                                      np.array([None] * i), {}, {})
            else:
                if ((original_prediction - best_cf_example.counterfactual_prediction) >
                        (original_prediction - prediction)):
                    best_cf_example = EvaluationCounterFactualExample(explained_event_id, original_prediction,
                                                                      prediction, False, subgraph_sample,
                                                                      np.array([None] * i), {}, {})

        self.tgnn.remove_memory_backup(EXPLAINED_EVENT_MEMORY_LABEL)
        self.tgnn.reset_model()
        return best_cf_example
