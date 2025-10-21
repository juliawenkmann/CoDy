import argparse
import logging
import time
import os
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd

from cody.constants import COL_ID, EXPLAINED_EVENT_MEMORY_LABEL
from time_to_explain.data.data import TrainTestDatasetParameters
from time_to_explain.data.common import (add_dataset_arguments, add_wrapper_model_arguments, create_dataset_from_args,
                    create_tgnn_wrapper_from_args, parse_args, get_event_ids_from_file, SAMPLERS, column_to_int_array,
                    column_to_float_array)

from scripts.evaluation_explainers import EvaluationExplainer, EvaluationCounterFactualExample, \
    EvaluationGreedyCFExplainer, EvaluationCoDy, EvaluationIRandExplainer
import scripts.evaluation_explainers
from time_to_explain.utils.utils import ProgressBar

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()


def evaluate(evaluated_explainers: List[EvaluationExplainer], explained_event_ids: np.ndarray, optimize: bool = False,
             max_time_seconds: int = 72 * 60):
    if explainers[0].tgnn.use_memory:
        evaluate_on_stateful(evaluated_explainers, explained_event_ids, optimize, max_time_seconds)
    else:
        evaluate_on_stateless(evaluated_explainers, explained_event_ids, max_time_seconds)


def evaluate_on_stateful(evaluated_explainers: List[EvaluationExplainer], explained_event_ids: np.ndarray,
                         optimize: bool = False, max_time_seconds: int = 72 * 60):
    assert len(evaluated_explainers) > 0
    progress_bar = ProgressBar(len(explained_event_ids), prefix='Evaluating explainer')
    start_time = time.time()
    base_explainer = explainers[0]
    tgnn = base_explainer.tgnn
    tgnn.set_evaluation_mode(True)
    memory_backups = {}

    if optimize:
        rollout_event_ids = {}
        for event_id in explained_event_ids:
            subgraph = base_explainer.subgraph_generator.get_fixed_size_k_hop_temporal_subgraph(base_explainer.num_hops,
                                                                                                event_id,
                                                                                                base_explainer.
                                                                                                candidates_size)
            rollout_event_id = subgraph[COL_ID].min() - 1
            rollout_event_ids[event_id] = rollout_event_id
        base_explainer.tgnn.reset_model()
        for rollout_event_id in sorted(set(rollout_event_ids.values())):
            last_batch_end_id = int(np.floor(rollout_event_id / tgnn.batch_size) * tgnn.batch_size) - 1
            tgnn.rollout_until_event(last_batch_end_id)
            last_batch_end_memory = tgnn.get_memory()
            tgnn.rollout_until_event(rollout_event_id)
            memory_backup = base_explainer.tgnn.get_memory()
            for event_id in [key for key, value in rollout_event_ids.items() if value == rollout_event_id]:
                memory_backups[event_id] = (rollout_event_id, memory_backup)
            tgnn.restore_memory(last_batch_end_memory, last_batch_end_id)

    for event_id in explained_event_ids:
        progress_bar.update_postfix(f'Generating original score for event {event_id}')
        if time.time() - start_time > max_time_seconds:
            logger.info("Time limit reached. Finishing evaluation...")
            break
        if optimize:
            tgnn.reset_model()
            restore_event_id, memory_backup = memory_backups[event_id]
            tgnn.memory_backups_map[EXPLAINED_EVENT_MEMORY_LABEL] = (memory_backup, restore_event_id)
            original_prediction = base_explainer.calculate_original_score(event_id, restore_event_id)
        else:
            original_prediction = None
        tgnn.reset_model()
        progress_bar.update_postfix(f'Generating explanation for event {event_id}')
        for selected_explainer in evaluated_explainers:
            explanation = selected_explainer.evaluate_explanation(event_id, original_prediction)
            selected_explainer.explanation_results_list.append(explanation)
            # Set the original prediction in the first iteration so that it does not have to be calculated again
            original_prediction = explanation.original_prediction
            if optimize:
                restore_event_id, memory_backup = memory_backups[event_id]
                tgnn.memory_backups_map[EXPLAINED_EVENT_MEMORY_LABEL] = (memory_backup, restore_event_id)
                tgnn.reset_model()
        scripts.evaluation_explainers.EVALUATION_STATE_CACHE = {}  # Reset the state cache
        progress_bar.next()
    progress_bar.close()


def evaluate_on_stateless(evaluated_explainers: List[EvaluationExplainer], explained_event_ids: np.ndarray,
                          max_time_seconds: int = 72 * 60):
    assert len(evaluated_explainers) > 0
    progress_bar = ProgressBar(len(explained_event_ids), prefix='Evaluating explainer')
    start_time = time.time()
    base_explainer = explainers[0]
    tgnn = base_explainer.tgnn
    tgnn.set_evaluation_mode(True)

    for event_id in explained_event_ids:
        if time.time() - start_time > max_time_seconds:
            logger.info("Time limit reached. Finishing evaluation...")
            break
        original_prediction = None
        tgnn.reset_model()
        progress_bar.update_postfix(f'Generating explanation for event {event_id}')
        for selected_explainer in evaluated_explainers:
            explanation = selected_explainer.evaluate_explanation(event_id, original_prediction)
            selected_explainer.explanation_results_list.append(explanation)
            # Set the original prediction in the first iteration so that it does not have to be calculated again
            original_prediction = explanation.original_prediction
        scripts.evaluation_explainers.EVALUATION_STATE_CACHE = {}  # Reset the state cache
        progress_bar.next()
    progress_bar.close()


def export_explanations(explanation_list: List[EvaluationCounterFactualExample], filepath: str):
    explanations_dicts = [explanation.to_dict() for explanation in explanation_list]
    explanations_df = pd.DataFrame(explanations_dicts)
    parquet_file_path = filepath.rstrip('csv') + 'parquet'
    if os.path.exists(parquet_file_path):
        existing_results = pd.read_parquet(parquet_file_path)
        explanations_df = pd.concat([existing_results, explanations_df], axis='rows')
    elif os.path.exists(filepath):
        existing_results = pd.read_csv(filepath)
        existing_results = existing_results.iloc[:, 1:]
        column_to_int_array(existing_results, 'cf_example_event_ids')
        column_to_int_array(existing_results, 'candidates')
        column_to_float_array(existing_results, 'cf_example_absolute_importances')
        column_to_float_array(existing_results, 'cf_example_raw_importances')
        explanations_df = pd.concat([existing_results, explanations_df], axis='rows')
    try:
        explanations_df.to_parquet(parquet_file_path)
        logger.info(f'Saved evaluation results to {parquet_file_path}')
    except ImportError:
        logger.info('Failed to export to parquet format. Install pyarrow to export to parquet format '
                    '(pip install pyarrow)')
    explanations_df.to_csv(filepath)
    logger.info(f'Saved evaluation results to {filepath}')


def construct_results_save_path(arguments: argparse.Namespace, eval_explainer: EvaluationExplainer):
    Path(arguments.results).mkdir(parents=True, exist_ok=True)
    if arguments.wrong_predictions_only:
        return (f'{arguments.results}/results_{arguments.type}_{eval_explainer.dataset.name}_{arguments.explainer}'
                f'_{eval_explainer.selection_policy}_wrong_only.csv')
    return (f'{arguments.results}/results_{arguments.type}_{eval_explainer.dataset.name}_{arguments.explainer}'
            f'_{eval_explainer.selection_policy}.csv')


if __name__ == '__main__':
    parser = argparse.ArgumentParser('Explainer Evaluation')
    add_dataset_arguments(parser)
    add_wrapper_model_arguments(parser)
    parser.add_argument('--explained_ids', required=True, type=str,
                        help='Path to the file containing all the event ids that should be explained')
    parser.add_argument('--wrong_predictions_only', action='store_true',
                        help='Provide if evaluation should focus on wrong predictions only')
    parser.add_argument('--debug', action='store_true',
                        help='Add this flag for more detailed debug outputs')
    parser.add_argument('--optimize', action='store_true',
                        help='Add this flag to optimize evaluation performance by pre computing memory resume '
                             'checkpoints')
    parser.add_argument('-r', '--results', required=True, type=str,
                        help='Filepath for the evaluation results')
    parser.add_argument('--explainer', required=True, type=str, help='Which explainer to evaluate',
                        choices=['greedy', 'cody', 'irand'])
    parser.add_argument('--sampler', required=True, default='recent', type=str,
                        choices=['random', 'temporal', 'spatio-temporal', 'local-gradient', 'all'])
    parser.add_argument('--dynamic', action='store_true',
                        help='Provide to indicate that dynamic embeddings should be used')
    parser.add_argument('--sample_size', type=int, default=10,
                        help='Number of samples to draw in each sampling step')
    parser.add_argument('--number_of_explained_events', type=int, default=1000,
                        help='Number of event ids to explain. Only has an effect if the explained_ids file has not '
                             'been initialized yet')
    parser.add_argument('--max_time', type=int, default=2400,
                        help='Maximal runtime (minutes)')
    parser.add_argument('--max_steps', type=int, default=100,
                        help='Maximum number of search steps to perform.')
    parser.add_argument('--no_approximation', action='store_true',
                        help='Provide if approximation should be disabled')
    parser.add_argument('--alpha', type=float, default=2/3)

    args = parse_args(parser)

    dataset = create_dataset_from_args(args, TrainTestDatasetParameters(0.2, 0.6, 0.8, args.number_of_explained_events,
                                                                        500, 500))

    tgn_wrapper = create_tgnn_wrapper_from_args(args, dataset)

    event_ids_to_explain = get_event_ids_from_file(args.explained_ids, logger, args.wrong_predictions_only,
                                                   tgn_wrapper)

    explainers = []
    match args.explainer:
        case 'greedy':
            if args.sampler == 'all':
                for sampler in SAMPLERS:
                    explainers.append(EvaluationGreedyCFExplainer(tgn_wrapper, selection_policy=sampler,
                                                                  candidates_size=args.candidates_size,
                                                                  sample_size=args.sample_size,
                                                                  verbose=args.debug,
                                                                  approximate_predictions=not args.no_approximation))
            else:
                explainers.append(EvaluationGreedyCFExplainer(tgn_wrapper, selection_policy=args.sampler,
                                                              candidates_size=args.candidates_size,
                                                              sample_size=args.sample_size,
                                                              verbose=args.debug,
                                                              approximate_predictions=not args.no_approximation))
        case 'cody':
            if args.sampler == 'all':
                for sampler in SAMPLERS:
                    explainers.append(EvaluationCoDy(tgn_wrapper, selection_policy=sampler,
                                                     candidates_size=args.candidates_size,
                                                     max_steps=args.max_steps, verbose=args.debug,
                                                     approximate_predictions=not args.no_approximation, alpha=args.alpha))
            else:
                explainers.append(EvaluationCoDy(tgn_wrapper, selection_policy=args.sampler,
                                                 candidates_size=args.candidates_size,
                                                 max_steps=args.max_steps, verbose=args.debug,
                                                 approximate_predictions=not args.no_approximation, alpha=args.alpha))
        case 'irand':
            explainers.append((EvaluationIRandExplainer(tgn_wrapper, candidates_size=args.candidates_size,
                                                        verbose=args.debug,
                                                        approximate_predictions=not args.no_approximation)))
        case _:
            raise NotImplementedError

    if os.path.exists(construct_results_save_path(args, explainers[0])):
        previous_results = pd.read_csv(construct_results_save_path(args, explainers[0]))
        encountered_event_ids = previous_results['explained_event_id'].to_numpy()
        logger.info(f'Resuming evaluation. '
                    f'Already processed {len(encountered_event_ids)}/{len(event_ids_to_explain)} events.')
        event_ids_to_explain = event_ids_to_explain[~np.isin(event_ids_to_explain, encountered_event_ids)]
    try:
        evaluate(explainers, event_ids_to_explain, args.optimize, args.max_time * 60)
    except KeyboardInterrupt:
        logger.info('Evaluation interrupted. Saving current results...')
    for explainer in explainers:
        if len(explainer.explanation_results_list) > 0:
            export_explanations(explainer.explanation_results_list, construct_results_save_path(args, explainer))
