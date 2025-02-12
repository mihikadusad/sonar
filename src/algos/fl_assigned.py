import numpy as np
import math
from typing import Any, Dict
from utils.communication.comm_utils import CommunicationManager
from algos.base_class import BaseFedAvgClient, BaseFedAvgServer

from utils.stats_utils import from_round_stats_per_round_per_client_to_dict_arrays


class FedAssClient(BaseFedAvgClient):
    def __init__(
        self, config: Dict[str, Any], comm_utils: CommunicationManager
    ) -> None:
        super().__init__(config, comm_utils=comm_utils)

    def get_collaborator_weights(self, num_collaborator, round):
        """
        Returns the weights of the collaborators for the current round
        """
        if self.config["strategy"] == "fixed":
            collab_weights = {
                id: 1 for id in self.config["assigned_collaborators"][self.node_id]
            }
        elif self.config["strategy"] == "direct_expo":
            power = round % math.floor(math.log2(self.config["num_users"] - 1))
            steps = math.pow(2, power)
            collab_id = int(((self.node_id + steps) % self.config["num_users"]) + 1)
            collab_weights = {self.node_id: 1, collab_id: 1}
        elif self.config["strategy"] == "random_among_assigned":
            collab_weights = {
                k: 1
                for k in np.random.choice(
                    list(self.config["assigned_collaborators"][self.node_id]),
                    size=num_collaborator,
                    replace=False,
                )
            }
            collab_weights[self.node_id] = 1
        else:
            raise ValueError("Strategy not implemented")

        total = sum(collab_weights.values())
        collab_weights = {id: w / total for id, w in collab_weights.items()}
        return collab_weights

    def get_representation(self):
        return self.get_model_weights()

    def run_protocol(self):
        print(f"Client {self.node_id} ready to start training")
        start_round = self.config.get("start_round", 0)
        total_rounds = self.config["rounds"]
        epochs_per_round = self.config["epochs_per_round"]
        for round in range(start_round, total_rounds):
            stats = {}

            # Wait on server to start the round
            self.comm_utils.receive(node_ids=self.server_node, tag=self.tag.ROUND_START)

            repr = self.get_representation()
            self.comm_utils.send(
                dest=self.server_node, data=repr, tag=self.tag.REPR_ADVERT
            )

            # Collect the representations from all other nodes from the server
            reprs = self.comm_utils.receive(
                node_ids=self.server_node, tag=self.tag.REPRS_SHARE
            )

            # In the future this dict might be generated by the server to send only requested models
            reprs_dict = {k: v for k, v in enumerate(reprs, 1)}

            num_collaborator = self.config[
                f"target_users_{'before' if round < self.config['T_0'] else 'after'}_T_0"
            ]

            # Aggregate the representations based on the collab weights
            collab_weights_dict = self.get_collaborator_weights(num_collaborator, round)

            collaborators = [k for k, w in collab_weights_dict.items() if w > 0]
            # If there are no collaborators, then the client does not update its model
            if not (len(collaborators) == 1 and collaborators[0] == self.node_id):
                # Since clients representations are also used to transmit knowledge
                # There is no need to fetch the server for the selected clients' knowledge
                models_wts = reprs_dict

                avg_wts = self.aggregate(
                    models_wts,
                    collab_weights_dict,
                    keys_to_ignore=self.model_keys_to_ignore,
                )

                # Average whole model by default
                self.set_model_weights(avg_wts, self.model_keys_to_ignore)

            stats["test_acc_before_training"] = self.local_test()

            stats["train_loss"], stats["train_acc"] = self.local_train(epochs_per_round)

            # Test updated model
            stats["test_acc_after_training"] = self.local_test()

            # Include collab weights in the stats
            collab_weight = np.zeros(self.config["num_users"])
            for k, v in collab_weights_dict.items():
                collab_weight[k - 1] = v
            stats["collab_weights"] = collab_weight
            self.comm_utils.send(
                dest=self.server_node, data=stats, tag=self.tag.ROUND_STATS
            )


class FedAssServer(BaseFedAvgServer):
    def __init__(
        self, config: Dict[str, Any], comm_utils: CommunicationManager
    ) -> None:
        super().__init__(config, comm_utils=comm_utils)
        # self.set_parameters()
        self.config = config
        self.set_model_parameters(config)
        self.model_save_path = "{}/saved_models/node_{}.pt".format(
            self.config["results_path"], self.node_id
        )

    def test(self) -> float:
        """
        Test the model on the server
        """
        test_loss, acc = self.model_utils.test(
            self.model, self._test_loader, self.loss_fn, self.device
        )
        # TODO save the model if the accuracy is better than the best accuracy so far
        if acc > self.best_acc:
            self.best_acc = acc
            self.model_utils.save_model(self.model, self.model_save_path)
        return acc

    def single_round(self):
        """
        Runs the whole training procedure
        """

        # Send signal to all clients to start local training
        for client_node in self.users:
            self.comm_utils.send(dest=client_node, data=None, tag=self.tag.ROUND_START)
        self.log_utils.log_console(
            "Server waiting for all clients to finish local training"
        )

        # Collect models from all clients
        models = self.comm_utils.all_gather(self.tag.REPR_ADVERT)
        self.log_utils.log_console("Server received all clients models")

        # Broadcast the models to all clients
        self.send_representations(models)

        # Collect round stats from all clients
        round_stats = self.comm_utils.all_gather(self.tag.ROUND_STATS)
        self.log_utils.log_console("Server received all clients stats")

        # Log the round stats on tensorboard except the collab weights
        self.log_utils.log_tb_round_stats(round_stats, ["collab_weights"], self.round)

        self.log_utils.log_console(
            f"Round acc TALT {[stats['test_acc_after_training'] for stats in round_stats]}"
        )
        self.log_utils.log_console(
            f"Round acc TBLT {[stats['test_acc_before_training'] for stats in round_stats]}"
        )

        return round_stats

    def run_protocol(self):
        self.log_utils.log_console("Starting random P2P collaboration")
        start_round = self.config.get("start_round", 0)
        total_round = self.config["rounds"]

        # List of list stats per round
        stats = []
        for round in range(start_round, total_round):
            self.round = round
            self.log_utils.log_console("Starting round {}".format(round))

            round_stats = self.single_round()
            stats.append(round_stats)

        stats_dict = from_round_stats_per_round_per_client_to_dict_arrays(stats)
        stats_dict["round_step"] = 1
        self.log_utils.log_experiments_stats(stats_dict)
        self.plot_utils.plot_experiments_stats(stats_dict)
