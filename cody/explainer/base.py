from __future__ import annotations

import logging
from typing import List

import numpy as np
import pandas as pd
from dataclasses import dataclass

from cody.implementations.connector import TGNNWrapper
from cody.constants import EXPLAINED_EVENT_MEMORY_LABEL, COL_ID
from time_to_explain.data.data import SubgraphGenerator
from cody.selection import (SelectionPolicy, RandomSelectionPolicy, TemporalSelectionPolicy,
                            SpatioTemporalSelectionPolicy, LocalEventImpactSelectionPolicy)


@dataclass
class CounterFactualExample:
    explained_event_id: int
    original_prediction: float
    counterfactual_prediction: float
    achieves_counterfactual_explanation: bool
    event_ids: np.ndarray
    event_importances: np.ndarray

    def __str__(self):
        return (f'Counterfactual example including events: {str(self.event_ids.tolist())}, original prediction '
                f'{str(self.original_prediction)}, counterfactual prediction {str(self.counterfactual_prediction)}, '
                f'event importances {str(self.get_relative_importances().tolist())}')

    def get_absolute_importances(self) -> np.ndarray:
        if len(self.event_importances) == 0 or self.event_importances[0] == None:
            return self.event_importances
        return np.array([importance - np.sum(self.event_importances[index - 1:index]) for index, importance in
                         enumerate(self.event_importances)])

    def get_relative_importances(self) -> np.ndarray:
        if len(self.event_importances) == 0:
            return np.ndarray([])
        return self.get_absolute_importances() / self.event_importances[-1]


class TreeNode:
    parent: TreeNode
    children: List[TreeNode]
    is_counterfactual: bool
    edge_id: int
    prediction: float | None
    original_prediction: float
    expanded: bool
    max_expansion_reached: bool
    exploitation_score: float

    def __init__(self, edge_id: int, parent: TreeNode | None, original_prediction: float):
        self.edge_id = edge_id
        self.parent = parent
        self.original_prediction = original_prediction
        self.prediction = None
        self.is_counterfactual = False
        self.expanded = False
        self.max_expansion_reached = False
        self.children = []
        self.exploitation_score = 0.0

    def _check_max_expanded(self):
        """
        Recursively check if the node is already maximally expanded, meaning that no further expansions of its child
        nodes are possible
        """
        if self.expanded:
            if not self.max_expansion_reached:
                for child in self.children:
                    if not child.max_expansion_reached:
                        return
                self.max_expansion_reached = True
            if self.parent is not None:
                self.parent._check_max_expanded()

    def is_leaf(self) -> bool:
        """
        Check if the node is a leaf node, meaning that it has no children
        """
        return len(self.children) == 0

    def expand(self, prediction: float, children: List[TreeNode]):
        """
        Expand the node
        @param prediction: The prediction achieved when this tree node is included in the counterfactual example
        @param children: List of children added in the expansion
        @return: None
        """
        self.prediction = prediction
        self.children.extend(children)
        self.exploitation_score = max(0.0, (calculate_prediction_delta(self.original_prediction, self.prediction) /
                                            abs(self.original_prediction)))
        self.expanded = True
        if self.original_prediction * self.prediction < 0:
            self.is_counterfactual = True
            self.max_expansion_reached = True
            if self.parent is not None:
                self.parent._check_max_expanded()
        if len(self.children) == 0:
            self.max_expansion_reached = True
            if self.parent is not None:
                self.parent._check_max_expanded()
        self.expansion_backpropagation()

    def select_next_leaf(self, max_depth: int) -> TreeNode:
        """
        Select the next leaf node for expansion
        @param max_depth: Maximum depth at which to search for leaf nodes
        @return: Leaf node to expand
        """
        raise NotImplementedError

    def expansion_backpropagation(self):
        """
        Propagate the information that a node is selected backwards and update scores
        """
        raise NotImplementedError

    def to_cf_example(self) -> CounterFactualExample:
        """
        Returns an instance of CounterFactualExample for the current node by aggregating information from parents
        """
        cf_events = []
        cf_event_importances = []
        node = self
        while node.parent is not None:
            cf_events.append(node.edge_id)
            cf_event_importances.append(calculate_prediction_delta(self.original_prediction, node.prediction))
            node = node.parent
        cf_events.reverse()
        cf_event_importances.reverse()
        return CounterFactualExample(explained_event_id=node.edge_id,
                                     original_prediction=self.original_prediction,
                                     counterfactual_prediction=self.prediction,
                                     achieves_counterfactual_explanation=self.is_counterfactual,
                                     event_ids=np.array(cf_events),
                                     event_importances=np.array(cf_event_importances))

    def get_parent_ids(self):
        parent_ids = []
        node = self
        while node.parent is not None:
            parent_ids.append(node.edge_id)
            node = node.parent
        return parent_ids

    def hash(self):
        edge_ids = []
        node = self
        while node.parent is not None:
            edge_ids.append(node.edge_id)
            node = node.parent
        sorted_edge_ids = sorted(edge_ids)
        return '-'.join(map(str, sorted_edge_ids))


def calculate_prediction_delta(original_prediction: float, prediction_to_assess: float) -> float:
    if prediction_to_assess * original_prediction < 0:
        return abs(prediction_to_assess) + abs(original_prediction)
    return abs(original_prediction) - abs(prediction_to_assess)


class Explainer:

    def __init__(self, tgnn_wrapper: TGNNWrapper, selection_policy: str = 'recent', candidates_size: int = 75,
                 sample_size: int = 10, verbose: bool = False, approximate_predictions: bool = True):
        self.tgnn = tgnn_wrapper
        self.dataset = self.tgnn.dataset
        self.subgraph_generator = SubgraphGenerator(self.dataset)
        self.num_hops = self.tgnn.num_hops
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger()
        self.selection_policy = selection_policy
        self.candidates_size = candidates_size
        self.sample_size = sample_size
        self.verbose = verbose
        self.approximate_predictions = approximate_predictions

    def _create_sampler(self, subgraph: pd.DataFrame, explained_event_id: int,
                        original_prediction: float) -> SelectionPolicy:
        """
        Create sampling according to
        @type subgraph: DataFrame The subgraph on which to create the sampling
        """
        if self.selection_policy == 'random':
            return RandomSelectionPolicy(subgraph)
        elif self.selection_policy == 'temporal':
            return TemporalSelectionPolicy(subgraph)
        elif self.selection_policy == 'spatio-temporal':
            return SpatioTemporalSelectionPolicy(subgraph)
        elif self.selection_policy == 'local-gradient':
            return LocalEventImpactSelectionPolicy(subgraph)
        else:
            raise NotImplementedError(f'No sampling implemented for selection policy {self.selection_policy}')

    def calculate_original_score(self, explained_event_id: int, min_event_id: int) -> float:
        """
        Calculate the original prediction for the full graph
        @param explained_event_id: Event that is explained
        @param min_event_id: Lowest event id in the candidate events
        @return: The prediction for the full graph without exclusions
        """
        self.tgnn.initialize(min_event_id, show_progress=self.verbose, memory_label=EXPLAINED_EVENT_MEMORY_LABEL)
        self.tgnn.initialize(explained_event_id - 1)
        original_prediction, _ = self.tgnn.predict(explained_event_id, result_as_logit=True)
        original_prediction = original_prediction.detach().cpu().item()
        if self.verbose:
            self.logger.info(f'Original prediction {original_prediction}')
        return original_prediction

    def initialize_explanation(self, explained_event_id: int) -> (float, SelectionPolicy):
        """
        Initialize the explanation process
        @param explained_event_id: ID of the event that should be explained
        @return: Original prediction for the event, EdgeSampler for the fixed-size-k-hop-temporal-subgraph around the
        explained event
        """
        subgraph = self.subgraph_generator.get_fixed_size_k_hop_temporal_subgraph(num_hops=self.num_hops,
                                                                                  base_event_id=explained_event_id,
                                                                                  size=self.candidates_size)
        min_event_id = subgraph[COL_ID].min() - 1  # One less since we do not want to simulate the minimal event

        self.tgnn.set_evaluation_mode(True)
        self.tgnn.reset_model()
        original_prediction = self.calculate_original_score(explained_event_id, min_event_id)
        return original_prediction, self._create_sampler(subgraph, explained_event_id, original_prediction)

    def calculate_subgraph_prediction(self, candidate_events: np.ndarray, cf_example_events: List[int],
                                      explained_event_id: int, candidate_event_id: int,
                                      original_prediction: float,
                                      memory_label: str = EXPLAINED_EVENT_MEMORY_LABEL) -> float:
        """
        Calculate the prediction score for the explained event, when excluding the candidate events
        @param candidate_events: Candidate events
        @param cf_example_events: Events to exclude
        @param explained_event_id: ID of the explained event
        @param candidate_event_id: ID of the currently investigated candidate event
        @param memory_label: Provide name of memory label if it should be different from the default
        @param original_prediction: Original prediction when considering all events
        @return: Prediction when excluding the candidate events
        """
        self.tgnn.initialize(np.min(candidate_events) - 1, show_progress=False,
                             memory_label=memory_label)
        full_cf_example_events = np.array(cf_example_events + [candidate_event_id])
        event_ids_to_rollout = None
        if self.approximate_predictions:
            event_ids_to_rollout = candidate_events[~np.isin(candidate_events, full_cf_example_events)]

        subgraph_pred, _ = self.tgnn.compute_edge_probabilities_for_subgraph(explained_event_id,
                                                                             full_cf_example_events,
                                                                             result_as_logit=True,
                                                                             event_ids_to_rollout=event_ids_to_rollout)
        if original_prediction * subgraph_pred < 0 and self.approximate_predictions:
            # Approximated prediction is counterfactual -> Get the true score
            self.tgnn.initialize(np.min(candidate_events) - 1, show_progress=False,
                                 memory_label=memory_label)
            subgraph_pred, _ = self.tgnn.compute_edge_probabilities_for_subgraph(explained_event_id,
                                                                                 full_cf_example_events,
                                                                                 result_as_logit=True,
                                                                                 event_ids_to_rollout=None)
        subgraph_pred = subgraph_pred.detach().cpu().item()
        return subgraph_pred

    def explain(self, explained_event_id: int) -> CounterFactualExample:
        """
        Explain the provided event
        @param explained_event_id: Event id to explain
        @return: The counterfactual explanation
        """
        raise NotImplementedError
