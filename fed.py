"""
This example involves learning using sensitive medical data from multiple hospitals
to predict diabetes progression in patients. The data is a standard dataset from
sklearn[1].

Recorded variables are:
- age,
- gender,
- body mass index,
- average blood pressure,
- and six blood serum measurements.

The target variable is a quantitative measure of the disease progression.
Since this measure is continuous, we solve the problem using linear regression.

The patients' data is split between 3 hospitals, all sharing the same features
but different entities. We refer to this scenario as horizontally partitioned.

The objective is to make use of the whole (virtual) training set to improve
upon the model that can be trained locally at each hospital.

50 patients will be kept as a test set and not used for training.

An additional agent is the 'server' who facilitates the information exchange
among the hospitals under the following privacy constraints:

1) The individual patient's record at each hospital cannot leave the premises,
   not even in encrypted form.
2) Information derived (read: gradients) from any hospital's dataset
   cannot be shared, unless it is first encrypted.
3) None of the parties (hospitals AND server) should be able to infer WHERE
   (in which hospital) a patient in the training set has been treated.

Note that we do not protect from inferring IF a particular patient's data
has been used during learning. Differential privacy could be used on top of
our protocol for addressing the problem. For simplicity, we do not discuss
it in this example.

In this example linear regression is solved by gradient descent. The server
creates a paillier public/private keypair and does not share the private key.
The hospital clients are given the public key. The protocol works as follows.
Until convergence: hospital 1 computes its gradient, encrypts it and sends it
to hospital 2; hospital 2 computes its gradient, encrypts and sums it to
hospital 1's; hospital 3 does the same and passes the overall sum to the
server. The server obtains the gradient of the whole (virtual) training set;
decrypts it and sends the gradient back - in the clear - to every client.
The clients then update their respective local models.

From the learning viewpoint, notice that we are NOT assuming that each
hospital sees an unbiased sample from the same patients' distribution:
hospitals could be geographically very distant or serve a diverse population.
We simulate this condition by sampling patients NOT uniformly at random,
but in a biased fashion.
The test set is instead an unbiased sample from the overall distribution.

From the security viewpoint, we consider all parties to be "honest but curious".
Even by seeing the aggregated gradient in the clear, no participant can pinpoint
where patients' data originated. This is true if this RING protocol is run by
at least 3 clients, which prevents reconstruction of each others' gradients
by simple difference.

This example was inspired by Google's work on secure protocols for federated
learning[2].

[1]: http://scikit-learn.org/stable/datasets/index.html#diabetes-dataset
[2]: https://research.googleblog.com/2017/04/federated-learning-collaborative.html

Dependencies: numpy, sklearn
"""
import asyncio
import random
from typing import Iterable

import numpy as np
from sklearn.datasets import load_diabetes

import phe as paillier

# from distro_paillier.source import distributed_paillier
# from distro_paillier.source.distributed_paillier import generate_shared_paillier_key

seed = 43
np.random.seed(seed)


def get_data(n_clients):
    """
    Import the dataset via sklearn, shuffle and split train/test.
    Return training, target lists for `n_clients` and a holdout test set
    """
    print("Loading data")
    diabetes = load_diabetes()
    y = diabetes.target
    X = diabetes.data
    # Add constant to emulate intercept
    X = np.c_[X, np.ones(X.shape[0])]

    # The features are already preprocessed
    # Shuffle
    perm = np.random.permutation(X.shape[0])
    X, y = X[perm, :], y[perm]

    # Select test at random
    test_size = 50
    test_idx = np.random.choice(X.shape[0], size=test_size, replace=False)
    train_idx = np.ones(X.shape[0], dtype=bool)
    train_idx[test_idx] = False
    X_test, y_test = X[test_idx, :], y[test_idx]
    X_train, y_train = X[train_idx, :], y[train_idx]

    # Split train among multiple clients.
    # The selection is not at random. We simulate the fact that each client
    # sees a potentially very different sample of patients.
    X, y = [], []
    step = int(X_train.shape[0] / n_clients)
    for c in range(n_clients):
        X.append(X_train[step * c: step * (c + 1), :])
        y.append(y_train[step * c: step * (c + 1)])

    return X, y, X_test, y_test


def mean_square_error(y_pred, y):
    """ 1/m * \sum_{i=1..m} (y_pred_i - y_i)^2 """
    return np.mean((y - y_pred) ** 2)


def encrypt_vector(public_key, x):
    return np.array([public_key.encrypt(i) for i in x])


def decrypt_vector(private_key, x):
    return np.array([private_key.decrypt(i) for i in x])


def sum_encrypted_vectors(x, y):
    if len(x) != len(y):
        raise ValueError('Encrypted vectors must have the same size')
    return x + y


class Server:
    """Private key holder. Decrypts the average gradient"""

    def __init__(self, key_length, n_clients):
        keypair = paillier.generate_paillier_keypair(n_length=key_length)
        self.pubkey, self.privkey = keypair

        # Key, pShares, qShares, N, PublicKey, LambdaShares, BetaShares, SecretKeyShares, theta = generate_shared_paillier_key(keyLength = key_length)

        # self.prikey = Key
        # self.pubkey = PublicKey
        # self.shares = SecretKeyShares
        # self.theta = theta

        self.n_clients = n_clients

    def aggregate_gradients(self, gradients: Iterable[np.array]):
        return np.sum(gradients, axis=0) / self.n_clients

    def decrypt_gradient(self, gradient):
        return decrypt_vector(self.privkey, gradient)
        # dec = np.array([
        #     self.prikey.decrypt(
        #         num, self.n_clients, distributed_paillier.CORRUPTION_THRESHOLD, self.pubkey, self.shares, self.theta
        #     )
        #     for num in gradient
        # ])
        # return dec


class Net:
    """
    Runs linear regression with local data or by gradient steps,
    where gradient can be passed in.
    """

    def __init__(self, X, y):
        self.X, self.y = X, y
        self.weights = np.zeros(X.shape[1])

    def predict(self, X):
        """Use model"""
        return X.dot(self.weights)

    def fit(self, n_iter, eta=0.01):
        """Linear regression for n_iter"""
        for _ in range(n_iter):
            gradient = self.compute_gradient()
            self.gradient_step(gradient, eta)

    def compute_gradient(self):
        """
        Compute the gradient of the current model using the training set
        """
        delta = self.predict(self.X) - self.y
        return delta.dot(self.X) / len(self.X)

    def gradient_step(self, gradient, eta=0.01):
        """Update the model with the given gradient"""
        self.weights -= eta * gradient


class Party:
    """
    Using public key can encrypt locally computed gradients.
    """
    def __init__(self, name, X, y, pubkey):
        self.name = name
        self.model = Net(X, y)
        self.pubkey = pubkey
  
    def get_noise(self):
        """
        Differential privacy simulation xD
        """
        return random.random() * 0.01
        # return 0

    async def compute_partial_gradient(self):
        """
        1. Compute gradient
        2. Add noise to it
        3. Encrypt it
        """
        gradient = self.model.compute_gradient()
        noisy_gradient = gradient + self.get_noise()
        encrypted_gradient = encrypt_vector(self.pubkey, noisy_gradient)
        return encrypted_gradient


async def hybrid_learning(X, y, X_test, y_test, config):
    """
    Performs learning with hybrid approach.
    """
    n_clients = config['n_clients']
    n_iter = config['n_iter']
    names = ['Hospital {}'.format(i) for i in range(1, n_clients + 1)]

    # Instantiate the server and generate private and public keys
    # NOTE: using smaller keys sizes wouldn't be cryptographically safe
    server = Server(key_length=config['key_length'], n_clients=n_clients)

    # Instantiate the clients.
    # Each client gets the public key at creation and its own local dataset
    clients = [
        Party(name, train_data, target_data, server.pubkey)
        for name, train_data, target_data in zip(names, X, y)
    ]

    # The federated learning with gradient descent
    print(f'Running distributed gradient aggregation for {n_iter} iterations')

    for i in range(n_iter):
        gradients = await asyncio.gather(
            *(
                client.compute_partial_gradient() for client in clients
            )
        )

        aggregate = server.aggregate_gradients(gradients)

        # Decrypted
        aggregate = server.decrypt_gradient(aggregate)

        # Take gradient steps
        for c in clients:
            c.model.gradient_step(aggregate, config['eta'])
        
        if i % 10 == 1:
            print(f'Epoch {i}')

    print('Error (MSE) that each client gets after running the protocol:')
    for c in clients:
        y_pred = c.model.predict(X_test)
        mse = mean_square_error(y_pred, y_test)
        print('{:s}:\t{:.2f}'.format(c.name, mse))


def local_learning(X, y, X_test, y_test, config):
    n_clients = config['n_clients']
    names = ['Hospital {}'.format(i) for i in range(1, n_clients + 1)]

    # Instantiate the clients.
    # Each client gets the public key at creation and its own local dataset
    clients = []
    for i in range(n_clients):
        clients.append(Party(names[i], X[i], y[i], None))

    # Each client trains a linear regressor on its own data
    print('Error (MSE) that each client gets on test set by '
          'training only on own local data:')
    for c in clients:
        c.model.fit(config['n_iter'], config['eta'])
        y_pred = c.model.predict(X_test)
        mse = mean_square_error(y_pred, y_test)
        print('{:s}:\t{:.2f}'.format(c.name, mse))


if __name__ == '__main__':
    config = {
        'n_clients': 5,
        'key_length': 1024,
        # 'n_clients': distributed_paillier.NUMBER_PLAYERS,
        # 'key_length': distributed_paillier.DEFAULT_KEYSIZE,
        'n_iter': 30,
        'eta': 1.5,
    }
    # load data, train/test split and split training data between clients
    X, y, X_test, y_test = get_data(n_clients=config['n_clients'])
    # first each hospital learns a model on its respective dataset for comparison.
    local_learning(X, y, X_test, y_test, config)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(hybrid_learning(X, y, X_test, y_test, config))
