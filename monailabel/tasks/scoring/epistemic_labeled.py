# Copyright (c) MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import torch
from monai.metrics.active_learning_metrics import LabelQualityScore, VarianceMetric

from monailabel.interfaces.datastore import Datastore
from monailabel.interfaces.tasks.infer import InferTask
from monailabel.interfaces.tasks.scoring import ScoringMethod

logger = logging.getLogger(__name__)


class EpistemicScoringWithLabels(ScoringMethod):
    """
    First version of Epistemic computation used as active learning strategy
    """

    def __init__(
        self,
        infer_task: InferTask,
        max_samples=0,
        simulation_size=5,
        key_output_entropy="epistemic_entropy",
        key_output_ts="epistemic_ts",
    ):
        super().__init__(f"Compute initial score based on dropout - {infer_task.description}")
        self.infer_task = infer_task
        self.dimension = infer_task.dimension

        self.max_samples = max_samples
        self.simulation_size = simulation_size
        self.key_output_entropy = key_output_entropy
        self.key_output_ts = key_output_ts

    def variance_volume(self, vol_input):
        vol_input = vol_input.astype(dtype="float32")
        variance_metric = VarianceMetric(include_background=True, threshold=0.0005, spatial_map=True)

        variance = variance_metric(vol_input)
        if self.dimension == 3:
            variance = np.expand_dims(variance, axis=0)
            variance = np.expand_dims(variance, axis=0)
        return variance

    def get_label_quality_score(self, vol_input, vol_label):

        vol_input = vol_input.astype(dtype="float32")
        vol_label = vol_label.astype(dtype="float32")
        label_qual_score = LabelQualityScore(include_background=True, reduction="sum")
        lq_score = label_qual_score(vol_input, vol_label)
        return lq_score

    def __call__(self, request, datastore: Datastore):
        logger.info("Starting Epistemic Uncertainty scoring")

        model_file = self.infer_task.get_path()
        model_ts = int(os.stat(model_file).st_mtime) if model_file and os.path.exists(model_file) else 1
        self.infer_task.clear_cache()

        # Performing Epistemic for all unlabeled images
        skipped = 0
        unlabeled_images = datastore.get_unlabeled_images()
        max_samples = request.get("max_samples", self.max_samples)
        simulation_size = request.get("simulation_size", self.simulation_size)
        if simulation_size < 2:
            simulation_size = 2
            logger.warning("EPISTEMIC:: Fixing 'simulation_size=2' as min 2 simulations are needed to compute entropy")

        logger.info(f"EPISTEMIC:: Total unlabeled images: {len(unlabeled_images)}; max_samples: {max_samples}")
        t_start = time.time()

        image_ids = []
        for image_id in unlabeled_images:
            image_info = datastore.get_image_info(image_id)
            prev_ts = image_info.get("epistemic_ts", 0)
            if prev_ts == model_ts:
                skipped += 1
                continue
            image_ids.append(image_id)
        image_ids = image_ids[:max_samples] if max_samples else image_ids

        max_workers = request.get("max_workers", 2)
        multi_gpu = request.get("multi_gpu", False)
        multi_gpus = request.get("gpus", "all")
        gpus = (
            list(range(torch.cuda.device_count())) if not multi_gpus or multi_gpus == "all" else multi_gpus.split(",")
        )
        device_ids = [f"cuda:{id}" for id in gpus] if multi_gpu else [request.get("device", "cuda")]

        max_workers = max_workers if max_workers else max(1, multiprocessing.cpu_count() // 2)
        max_workers = min(max_workers, multiprocessing.cpu_count())

        if len(image_ids) > 1 and (max_workers == 0 or max_workers > 1):
            logger.info(f"MultiGpu: {multi_gpu}; Using Device(s): {device_ids}; Max Workers: {max_workers}")
            futures = []
            with ThreadPoolExecutor(max_workers if max_workers else None, "ScoreInfer") as e:
                for image_id in image_ids:
                    futures.append(e.submit(self.run_scoring, image_id, simulation_size, model_ts, datastore))
                for future in futures:
                    future.result()
        else:
            for image_id in image_ids:
                self.run_scoring(image_id, simulation_size, model_ts, datastore)

        summary = {
            "total": len(unlabeled_images),
            "skipped": skipped,
            "executed": len(image_ids),
            "latency": round(time.time() - t_start, 3),
        }

        logger.info(f"EPISTEMIC:: {summary}")
        self.infer_task.clear_cache()
        return summary

    def run_scoring(self, image_id, simulation_size, model_ts, datastore):
        start = time.time()
        request = {
            "image": datastore.get_image_uri(image_id),
            "logging": "error",
            "cache_transforms": False,
        }

        accum_unl_outputs = []
        for i in range(simulation_size):
            data = self.infer_task(request=request)
            pred = data[self.infer_task.output_label_key] if isinstance(data, dict) else None
            if pred is not None:
                logger.debug(f"EPISTEMIC:: {image_id} => {i} => pred: {pred.shape}; sum: {np.sum(pred)}")
                accum_unl_outputs.append(pred)
            else:
                logger.info(f"EPISTEMIC:: {image_id} => {i} => pred: None")

        accum_numpy = np.stack(accum_unl_outputs)
        accum_numpy = np.squeeze(accum_numpy)
        if self.dimension == 3:
            accum_numpy = accum_numpy[:, 1:, :, :, :] if len(accum_numpy.shape) > 4 else accum_numpy
        else:
            accum_numpy = accum_numpy[:, 1:, :, :] if len(accum_numpy.shape) > 3 else accum_numpy

        entropy = self.variance_volume(accum_numpy)
        entropy = float(np.nanmean(entropy))

        # TODO @Sachi how do we get the ground_truth_label from the datastore here?
        lq_score = self.get_label_quality_score(pred, ground_truth_label)

        latency = time.time() - start
        logger.info(
            "EPISTEMIC:: {} => iters: {}; entropy: {}; latency: {};".format(
                image_id,
                simulation_size,
                round(entropy, 4),
                round(latency, 3),
            )
        )

        # Add epistemic_entropy in datastore
        # TODO @Sachi We can consider adding the label quality score here in the below dictionary
        info = {self.key_output_entropy: entropy, self.key_output_ts: model_ts}
        datastore.update_image_info(image_id, info)

        # TODO @Sachi The Next thing that we need to do is for combining both Entropy & Varince to a single score is normalization
        # Step 1: Normalize all values of entropy to a range of [0, 1]
        # Step 2: Normalize all values of label quality score to a range of [0, 1]
        # Step 3: The Final score becomes = Entropy - Label_Qual_Score
        # Step 4: We rank data based on the above score
