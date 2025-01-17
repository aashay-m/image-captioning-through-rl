###################################################
# Image Captioning with Deep Reinforcement Learning
# SJSU CMPE-297-03 | Spring 2020
#
#
# Team:
# Pratikkumar Prajapati
# Aashay Mokadam
# Karthik Munipalle
###################################################

import time
import random
import math
import torch.optim as optim
from tqdm import tqdm
from utilities import *
from models import *
from torch.utils.tensorboard import SummaryWriter


# https://cs230-stanford.github.io/pytorch-nlp.html#writing-a-custom-loss-function
def VisualSemanticEmbeddingLoss(visuals, semantics):
    """

    @param visuals: embedded features of the image
    @param semantics: embedded features of the caption
    @return: numerical loss based on similarity of embedded features

    Computes a joint loss by on visual data (CNN outputs) and semantic data (final state of RNN) by:
    1) First, fixing the image and:
        (i)  maximizing the similarity of the caption representation to the image
        (ii) minimizing the similarity of representations of negative captions to the image
    2) And then, fixing the caption and:
        (i)  maximizing the similarity of the image representation to the caption
        (ii) minimizing the similarity of representations of negative images to the caption
    """

    beta = 0.2
    N, D = visuals.shape

    visloss = torch.mm(visuals, semantics.t())
    visloss = visloss - torch.diag(visloss).unsqueeze(1)
    visloss = visloss + (beta / N) * (torch.ones((N, N)).to(device) - torch.eye(N).to(device))
    visloss = F.relu(visloss)
    visloss = torch.sum(visloss) / N

    semloss = torch.mm(semantics, visuals.t())
    semloss = semloss - torch.diag(semloss).unsqueeze(1)
    semloss = semloss + (beta / N) * (torch.ones((N, N)).to(device) - torch.eye(N).to(device))
    semloss = F.relu(semloss)
    semloss = torch.sum(semloss) / N

    return visloss + semloss


def GenerateCaptionsGreedy(features, captions, policy_network):
    """

    @param features: image features
    @param captions: image caption
    @param policy_network: network that decides on the next word
    @return: potential caption based on short-term greedy decision making
    """
    features = torch.tensor(features, device=device).float().unsqueeze(0)
    gen_caps = torch.tensor(captions[:, 0:1], device=device).long()
    for t in range(MAX_SEQ_LEN - 1):
        output = policy_network(features, gen_caps)
        gen_caps = torch.cat((gen_caps, output[:, -1:, :].argmax(axis=2)), axis=1)
    return gen_caps


def GenerateCaptionsWithActorCriticLookAhead(features, captions, policy_network, value_network, beamSize=5,
                                             most_likely=False):
    """

    @param features: image features
    @param captions: image caption
    @param policy_network: network that decides on the next word
    @param value_network: network that provides feedback on the global value (expected reward) for the next word
    @param beamSize: number of lookahead positions to consider when scoring potential captions
    @param most_likely: flag - whether to return the single most likely caption
    @return: list of potential captions
    """
    features = torch.tensor(features, device=device).float().unsqueeze(0)
    gen_caps = torch.tensor(captions[:, 0:1], device=device).long()

    candidates = [(gen_caps, 0)]
    for t in range(MAX_SEQ_LEN - 1):
        next_candidates = []
        for c in range(len(candidates)):
            output = policy_network(features, candidates[c][0])
            probs, words = torch.topk(output[:, -1:, :], beamSize)
            for i in range(beamSize):
                cap = torch.cat((candidates[c][0], words[:, :, i]), axis=1)
                value = value_network(features.squeeze(0), cap).detach()
                score_delta = 0.6 * value + 0.4 * torch.log(probs[:, :, i])
                score = candidates[c][1] - score_delta
                next_candidates.append((cap, score))
        ordered_candidates = sorted(next_candidates, key=lambda tup: tup[1].mean())
        candidates = ordered_candidates[:beamSize]

    if most_likely == True:
        return candidates[0][0]
    return candidates


def GetRewards(features, captions, reward_network):
    """

    @param features: image features
    @param captions: image caption
    @param reward_network: network that projects captions and images onto a common vector space
    @return: similarity between embedded projections of captions and images (cosine similarity)
    """
    visEmbeds, semEmbeds = reward_network(features, captions)
    visEmbeds = F.normalize(visEmbeds, p=2, dim=1)
    semEmbeds = F.normalize(semEmbeds, p=2, dim=1)

    rewards = torch.sum(visEmbeds * semEmbeds, axis=1).unsqueeze(1)
    return rewards


# Used https://github.com/Pranshu258/Deep_Image_Captioning as some of the code reference
def train_value_network(train_data, network_paths, plot_dir, bidirectional, epochs=50, batch_size=512):
    """
    Function to train value net. Trained on Mean-Squared Error Loss.

    @param train_data: the training data
    @param network_paths: path to store net
    @param plot_dir: path to store tensorboard graphs
    @param bidirectional: whether to use bidirectional recurrent networks
    @param epochs: num of epochs
    @param batch_size: batch size of data per epoch
    @return: the trained value network
    """

    value_writer = SummaryWriter(log_dir=os.path.join(plot_dir, 'runs'))

    reward_network = RewardNetwork(train_data["word_to_idx"], pretrained_embeddings=train_data["embeddings"],
                                   bidirectional=bidirectional).to(device)
    reward_network.load_state_dict(torch.load(network_paths["reward_network"], map_location=device), strict=False)
    reward_network.train(False)
    reward_network.requires_grad_(False)

    policy_network = PolicyNetwork(train_data["word_to_idx"], pretrained_embeddings=train_data["embeddings"],
                                   bidirectional=bidirectional).to(device)
    policy_network.load_state_dict(torch.load(network_paths["policy_network"], map_location=device), strict=False)
    policy_network.train(False)
    policy_network.requires_grad_(False)

    value_network = ValueNetwork(train_data["word_to_idx"], pretrained_embeddings=train_data["embeddings"],
                                 bidirectional=bidirectional).to(device)
    criterion = nn.MSELoss().to(device)
    optimizer = optim.Adam(value_network.parameters(), lr=0.001)
    value_network.train(mode=True)

    best_loss = float('inf')
    print_green(f'[Training] Training Value Network')

    for epoch in range(epochs):
        batch_progress = tqdm(get_coco_minibatches(train_data, batch_size=batch_size, split='train'),
                              total=math.ceil(train_data['train_captions'].shape[0] / batch_size),
                              desc='Training Value Network (%s/%s): Best Loss %s' % (epoch + 1, epochs, best_loss))
        for minibatch_id, coco_minibatch in enumerate(batch_progress):

            captions, features, _ = coco_minibatch
            features = torch.tensor(features, device=device).float()

            # Generate captions using the policy network
            captions = GenerateCaptionsGreedy(features, captions, policy_network)

            # Compute the reward of the generated caption using reward network
            rewards = GetRewards(features, captions, reward_network)

            # Compute the value of a random state in the generation process
            values = value_network(features, captions[:, :random.randint(1, MAX_SEQ_LEN)])

            # Compute the loss for the value and the reward
            loss = criterion(values, rewards)

            if loss.item() < best_loss:
                best_loss = loss.item()
                torch.save(value_network.state_dict(), network_paths["value_network"])
                batch_progress.set_description_str(
                    'Training Value Network (%s/%s): Best Loss %s' % (epoch + 1, epochs, best_loss))

            minibatch_number = global_minibatch_number(epoch, minibatch_id, batch_size)
            value_writer.add_scalar('Value Network-loss', loss, minibatch_number)

            optimizer.zero_grad()
            loss.backward(retain_graph=True)
            optimizer.step()

            # value_network.valrnn.hidden_cell = repackage_hidden(value_network.valrnn.hidden_cell)
            value_network.valrnn.init_hidden()
            reward_network.rewrnn.init_hidden()

    return value_network


def train_policy_network(train_data, network_paths, plot_dir, bidirectional, epochs=100, batch_size=512):
    """
    Function to train policy net. Trained on Cross Entropy Loss.

    @param train_data: the training data
    @param network_paths: path to store net
    @param plot_dir: path to store tensorboard graphs
    @param bidirectional: whether to use bidirectional recurrent networks
    @param epochs: num of epochs
    @param batch_size: batch size of data per epoch
    @return: the trained policy network
    """

    policy_network = PolicyNetwork(train_data["word_to_idx"], pretrained_embeddings=train_data["embeddings"],
                                   bidirectional=bidirectional).to(device)
    criterion = nn.CrossEntropyLoss().to(device)
    optimizer = optim.Adam(policy_network.parameters(), lr=0.001)

    policy_writer = SummaryWriter(log_dir=os.path.join(plot_dir, 'runs'))

    best_loss = float("inf")
    print_green(f'[Training] Training Policy Network')

    for epoch in range(epochs):

        batch_progress = tqdm(get_coco_minibatches(train_data, batch_size=batch_size, split='train'),
                              total=math.ceil(train_data['train_captions'].shape[0] / batch_size),
                              desc='Training Policy Network (%s/%s): Best Loss %s' % (epoch + 1, epochs, best_loss))
        for minibatch_id, coco_minibatch in enumerate(batch_progress):

            captions, features, _ = coco_minibatch
            features = torch.tensor(features, device=device).float().unsqueeze(0)
            captions_in = torch.tensor(captions[:, :-1], device=device).long()
            captions_out = torch.tensor(captions[:, 1:], device=device).long()
            output = policy_network(features, captions_in)

            loss = 0
            for i in range(captions.shape[0]):
                # '2' is the end of segment, hence points to caption length
                caplen = np.nonzero(captions[i] == 2)[0][0] + 1
                loss += (caplen / captions.shape[0]) * criterion(output[i][:caplen], captions_out[i][:caplen])

            if loss.item() < best_loss:
                best_loss = loss.item()
                torch.save(policy_network.state_dict(), network_paths["policy_network"])
                batch_progress.set_description_str(
                    'Training Policy Network (%s/%s): Best Loss %s' % (epoch + 1, epochs, best_loss))

            minibatch_number = global_minibatch_number(epoch, minibatch_id, batch_size)
            policy_writer.add_scalar('Policy Network-loss', loss, minibatch_number)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

    return policy_network


def train_reward_network(train_data, network_paths, plot_dir, bidirectional, epochs=50, batch_size=512):
    """
    Function to train reward net. Trained on Visual Semantic Embedding Loss.

    @param train_data: the training data
    @param network_paths: path to store net
    @param plot_dir: path to store tensorboard graphs
    @param bidirectional: whether to use bidirectional recurrent networks
    @param epochs: num of epochs
    @param batch_size: batch size of data per epoch
    @return: the trained reward network
    """
    reward_writer = SummaryWriter(log_dir=os.path.join(plot_dir, 'runs'))
    reward_network = RewardNetwork(train_data["word_to_idx"], pretrained_embeddings=train_data["embeddings"],
                                   bidirectional=bidirectional).to(device)
    optimizer = optim.Adam(reward_network.parameters(), lr=0.0001)

    best_loss = float('inf')
    print_green(f'[Training] Training Reward Network')

    for epoch in range(epochs):

        batch_progress = tqdm(get_coco_minibatches(train_data, batch_size=batch_size, split='train'),
                              total=math.ceil(train_data['train_captions'].shape[0] / batch_size),
                              desc='Training Reward Network (%s/%s): Best Loss %s' % (epoch + 1, epochs, best_loss))
        for minibatch_id, coco_minibatch in enumerate(batch_progress):

            captions, features, _ = coco_minibatch
            features = torch.tensor(features, device=device).float()
            captions = torch.tensor(captions, device=device).long()
            ve, se = reward_network(features, captions)
            loss = VisualSemanticEmbeddingLoss(ve, se)

            if loss.item() < best_loss:
                best_loss = loss.item()
                torch.save(reward_network.state_dict(), network_paths["reward_network"])
                batch_progress.set_description_str(
                    'Training Reward Network (%s/%s): Best Loss %s' % (epoch + 1, epochs, best_loss))

            minibatch_number = global_minibatch_number(epoch, minibatch_id, batch_size)
            reward_writer.add_scalar('Reward Network-loss', loss, minibatch_number)

            optimizer.zero_grad()
            loss.backward(retain_graph=True)
            optimizer.step()

            # reward_network.rewrnn.hidden_cell = repackage_hidden(reward_network.rewrnn.hidden_cell)
            reward_network.rewrnn.init_hidden()

    return reward_network


def train_a2c_network(train_data, save_paths, network_paths, plot_dir, bidirectional, epochs, batch_size,
                      retrain_all=False, curriculum=None):
    """
    Wrapper function to call actual training functions based on input configurations

    @param train_data: the dataset for training
    @param save_paths: path to store results
    @param network_paths:  path to load/store nets
    @param plot_dir:  path to store tensorboard graphs
    @param bidirectional: whether to use bidirectional recurrent networks
    @param epochs: the number of epochs for data passes
    @param batch_size:  batch size for each epoch
    @param retrain_all: whether to retrain all nets or laod pretrained nets
    @param curriculum: curriculum levels
    @return: the trained actor-critic network
    """
    model_save_path = save_paths["model_path"]
    results_save_path = save_paths["results_path"]

    if retrain_all:
        print_green(f'[Training] Training all the networks')
        reward_network = train_reward_network(train_data, network_paths, plot_dir, bidirectional, batch_size=batch_size)
        policy_network = train_policy_network(train_data, network_paths, plot_dir, bidirectional, batch_size=batch_size)
        value_network = train_value_network(train_data, network_paths, plot_dir, bidirectional, batch_size=batch_size)
        print_green(f'[Training] All networks trained')

    else:
        try:
            reward_network = RewardNetwork(train_data["word_to_idx"], pretrained_embeddings=train_data["embeddings"],
                                           bidirectional=bidirectional).to(device)
            reward_network.load_state_dict(torch.load(network_paths["reward_network"], map_location=device),
                                           strict=False)
            print(f'[Training] loaded reward network')
        except FileNotFoundError:
            print(f'[Training] reward network not found')
            del reward_network
            reward_network = train_reward_network(train_data, network_paths, plot_dir, bidirectional,
                                                  batch_size=batch_size)
        try:
            policy_network = PolicyNetwork(train_data["word_to_idx"], pretrained_embeddings=train_data["embeddings"],
                                           bidirectional=bidirectional).to(device)
            policy_network.load_state_dict(torch.load(network_paths["policy_network"], map_location=device),
                                           strict=False)
            print(f'[Training] loaded policy network')
        except FileNotFoundError:
            del policy_network
            print(f'[Training] policy network not found')
            policy_network = train_policy_network(train_data, network_paths, plot_dir, bidirectional,
                                                  batch_size=batch_size)
        try:
            value_network = ValueNetwork(train_data["word_to_idx"], pretrained_embeddings=train_data["embeddings"],
                                         bidirectional=bidirectional).to(device)
            value_network.load_state_dict(torch.load(network_paths["value_network"], map_location=device), strict=False)
            print(f'[Training] loaded value network')
        except FileNotFoundError:
            del value_network
            print(f'[Training] value network not found')
            value_network = train_value_network(train_data, network_paths, plot_dir, bidirectional,
                                                batch_size=batch_size)

    reward_network.requires_grad_(False)
    reward_network.train(False)

    a2c_network = AdvantageActorCriticNetwork(value_network, policy_network).to(device)
    a2c_network.train(True)

    optimizer = optim.Adam(a2c_network.parameters(), lr=0.0001)

    print(f'[Training] train_data len = {len(train_data["train_captions"])}')
    print(f'[Training] episodes = {batch_size}')
    print(f'[Training] epochs = {epochs}')

    save_paths = [model_save_path, network_paths["a2c_network"]]
    if curriculum is None:
        a2c_network = a2c_training(train_data, a2c_network, reward_network, optimizer, plot_dir, save_paths, batch_size,
                                   epochs)
    else:
        if 16 not in curriculum:
            curriculum.append(16)  # Final Curriculum Level, ie Full Training
        a2c_network = a2c_curriculum_training(train_data, a2c_network, reward_network, optimizer, plot_dir, save_paths,
                                              batch_size, epochs, curriculum)

    with open(results_save_path, 'a') as f:
        f.write('\n' + '-' * 10 + ' network ' + '-' * 10 + '\n')
        f.write(str(a2c_network))
        f.write('\n' + '-' * 10 + ' network ' + '-' * 10 + '\n')

    return a2c_network


def a2c_training(train_data, a2c_network, reward_network, optimizer, plot_dir, save_paths, batch_size, epochs):
    """
    Train the a2c model. Trained on Advantage-Weighted Log Probability Loss.

    @param train_data: the dataset for training
    @param a2c_network: the a2c network
    @param reward_network: the reward net for predicting rewards
    @param optimizer: the optimizer of the network
    @param plot_dir: path to store tensorboard graphs
    @param save_paths: path to save trained nets
    @param batch_size: batch size for each epoch
    @param epochs: the number of epochs for data passes
    @return: the trained actor-critic network
    """
    a2c_train_writer = SummaryWriter(log_dir=os.path.join(plot_dir, 'runs'))

    print_green(f'[Training] Training Advantage Actor-Critic Network')
    best_loss = float('inf')

    for epoch in range(epochs):

        batch_progress = tqdm(get_coco_minibatches(train_data, batch_size=batch_size, split='train'),
                              total=math.ceil(train_data['train_captions'].shape[0] / batch_size),
                              desc='Training A2C Network (%s/%s): Best Loss %s' % (epoch + 1, epochs, best_loss))
        for minibatch_id, coco_minibatch in enumerate(batch_progress):

            captions, features, _ = coco_minibatch
            features = torch.tensor(features, device=device).float()
            captions = torch.tensor(captions, device=device).long()

            rewards = []
            values = []
            log_probs = []

            caplen = np.nonzero(captions == 2)[:, 1].max() + 1

            captions_in = captions[:, :1]
            features_in = features

            for step in range(caplen - 1):

                value, probs = a2c_network(features_in, captions_in)
                probs = F.softmax(probs, dim=2)
                dist = probs.cpu().detach().numpy()[:, 0]

                actions = []
                for i in range(dist.shape[0]):
                    actions.append(np.random.choice(probs.shape[-1], p=dist[i]))
                actions = torch.from_numpy(np.array(actions))

                gen_cap = actions.unsqueeze(-1).to(device)
                try:
                    captions_in = torch.cat((captions_in, gen_cap), axis=1)
                except:
                    captions_in = torch.cat((captions_in, gen_cap.long()), axis=1)

                log_prob = torch.log(probs[:, 0, :].gather(1, actions.view(-1, 1).to(device)))
                reward = GetRewards(features_in, captions_in, reward_network)

                rewards.append(reward)
                values.append(value)
                log_probs.append(log_prob)

                del gen_cap, probs, actions, dist

            values = torch.stack(values, axis=1).squeeze().to(device)
            rewards = torch.stack(rewards, axis=1).squeeze().to(device)
            log_probs = torch.stack(log_probs, axis=1).squeeze().to(device)

            advantage = values - rewards
            actorLoss = (-log_probs * advantage).mean()
            criticLoss = 0.5 * advantage.pow(2).mean()

            loss = actorLoss + criticLoss
            episodic_avg_loss = loss.mean().item()

            optimizer.zero_grad()
            loss.mean().backward(retain_graph=True)
            optimizer.step()

            if episodic_avg_loss < best_loss:
                best_loss = episodic_avg_loss
                batch_progress.set_description_str(
                    'Training A2C Network (%s/%s): Best Loss %s' % (epoch + 1, epochs, best_loss))

            # Summary Writer
            minibatch_number = global_minibatch_number(epoch, minibatch_id, batch_size)
            a2c_train_writer.add_scalar('A2C Network-episodic-loss', episodic_avg_loss, minibatch_number)
            a2c_train_writer.add_scalar('A2C Network-episodic-mean-rewards', rewards.mean(), minibatch_number)
            a2c_train_writer.add_scalar('A2C Network-episodic-mean-advantage', advantage.mean().item(),
                                        minibatch_number)

            # a2c_network.value_network.valrnn.hidden_cell = repackage_hidden(a2c_network.value_network.valrnn.hidden_cell)
            reward_network.rewrnn.init_hidden()
            a2c_network.value_network.valrnn.init_hidden()

        save_a2c_model(a2c_network, save_paths)

    return a2c_network


def a2c_curriculum_training(train_data, a2c_network, reward_network, optimizer, plot_dir, save_paths, batch_size,
                            epochs, curriculum):
    """
    Train the model based on Curriculum Learning. 
    Start out training on the last few words of each caption, and increase the
    size of the caption the model should predict until the full caption is trained using 
    Reinforcement Learning.
    Trained on Advantage-Weighted Log Probability Loss.

    @param train_data: the dataset for training
    @param a2c_network: the a2c network
    @param reward_network: the reward net for predicting rewards
    @param optimizer: the optimizer of the network
    @param plot_dir: path to store tensorboard graphs
    @param save_paths: path to save trained nets
    @param batch_size: batch size for each epoch
    @param epochs: the number of epochs for data passes
    @param curriculum: curriculum levels
    @return: the trained actor-critic network
    """
    a2c_train_curriculum_writer = SummaryWriter(log_dir=os.path.join(plot_dir, 'runs'))

    print_green(f'[Training] Training Advantage Actor-Critic Network')
    print_green(f'[Training] mode set to curriculum training using levels: {curriculum}')

    for level in curriculum:
        print_green(f'[Training] Training curriculum level: {level}')
        best_loss = float('inf')

        for epoch in range(epochs):

            batch_progress = tqdm(get_coco_minibatches(train_data, batch_size=batch_size, split='train'),
                                  total=math.ceil(train_data['train_captions'].shape[0] / batch_size),
                                  desc='Training A2C Curriculum Level %s (%s/%s): Best Loss: %s' % (
                                  level, epoch, epochs, best_loss))
            for minibatch_id, coco_minibatch in enumerate(batch_progress):

                captions, features, _ = coco_minibatch
                features = torch.tensor(features, device=device).float()
                captions = torch.tensor(captions, device=device).long()

                log_probs = []
                values = []
                rewards = []
                caplen = np.nonzero(captions == 2)[:, 1].max() + 1
                curr_seq_len = caplen - level

                if (curr_seq_len >= 1):
                    captions_in = captions[:, :curr_seq_len]
                    features_in = features

                    for step in range(level):
                        value, probs = a2c_network(features_in, captions_in)
                        probs = F.softmax(probs, dim=2)

                        dist = probs.cpu().detach().numpy()[:, 0]
                        actions = []
                        for i in range(dist.shape[0]):
                            actions.append(np.random.choice(probs.shape[-1], p=dist[i]))
                        actions = torch.from_numpy(np.array(actions))

                        gen_cap = actions.unsqueeze(-1).to(device)
                        captions_in = torch.cat((captions_in, gen_cap), axis=1)
                        log_prob = torch.log(probs[:, 0, :].gather(1, actions.view(-1, 1).to(device)))

                        reward = GetRewards(features_in, captions_in, reward_network)

                        rewards.append(reward)
                        values.append(value)
                        log_probs.append(log_prob)

                        del gen_cap, probs, actions, dist

                    values = torch.stack(values, axis=1).squeeze().to(device)
                    rewards = torch.stack(rewards, axis=1).squeeze().to(device)
                    log_probs = torch.stack(log_probs, axis=1).squeeze().to(device)

                    advantage = values - rewards
                    actorLoss = (-log_probs * advantage).mean(axis=1)
                    criticLoss = 0.5 * advantage.pow(2).mean(axis=1)

                    loss = actorLoss + criticLoss
                    episodic_avg_loss = loss.mean().item()

                    if episodic_avg_loss < best_loss:
                        best_loss = episodic_avg_loss
                        batch_progress.set_description_str('Training A2C Curriculum Level %s (%s/%s): Best Loss: %s' % (
                        level, epoch, epochs, best_loss))

                    optimizer.zero_grad()
                    loss.mean().backward(retain_graph=True)
                    optimizer.step()

                    # Summary Writer
                    minibatch_number = global_minibatch_number(epoch, minibatch_id, batch_size)
                    writer_var_name = 'A2C Curriculum' + ' Level-' + str(level) + '-loss'
                    a2c_train_curriculum_writer.add_scalar(writer_var_name, episodic_avg_loss, minibatch_number)
                    writer_var_name = 'A2C Curriculum' + ' Level-' + str(level) + '-mean-rewards'
                    a2c_train_curriculum_writer.add_scalar(writer_var_name, rewards.mean(), minibatch_number)
                    writer_var_name = 'A2C Curriculum' + ' Level-' + str(level) + '-mean-advantage'
                    a2c_train_curriculum_writer.add_scalar(writer_var_name, advantage.mean().item(), minibatch_number)

                    log_probs.detach()
                    values.detach()
                    rewards.detach()
                del log_probs, values, rewards

                # a2c_network.value_network.valrnn.hidden_cell = repackage_hidden(a2c_network.value_network.valrnn.hidden_cell)
                reward_network.rewrnn.init_hidden()
                a2c_network.value_network.valrnn.init_hidden()

            save_a2c_model(a2c_network, save_paths)

    return a2c_network


def test_a2c_network(a2c_network, test_data, image_caption_data, data_size, validation_batch_size=128):
    """
    Function to test the a2c network
    @param a2c_network: the a2c network
    @param test_data: the dataset for testing
    @param image_caption_data: paths to store results
    @param data_size: size of the test data
    @param validation_batch_size: batch size to sample the data
    """
    with torch.no_grad():
        a2c_network.train(False)

        real_captions_filename = image_caption_data["real_captions_path"]
        generated_captions_filename = image_caption_data["generated_captions_path"]
        image_url_filename = image_caption_data["image_urls_path"]

        real_captions_file = open(real_captions_filename, "a")
        generated_captions_file = open(generated_captions_filename, "a")
        image_url_file = open(image_url_filename, "a")

        captions_real_all, features_real_all, urls_all = get_coco_batch(test_data, batch_size=data_size, split='val')
        val_captions_lens = len(captions_real_all)

        for i in tqdm(range(0, val_captions_lens, validation_batch_size), desc='Testing model'):
            features_real = features_real_all[i:i + validation_batch_size - 1]
            captions_real = captions_real_all[i:i + validation_batch_size - 1]
            urls = urls_all[i:i + validation_batch_size - 1]

            gen_cap = GenerateCaptionsWithActorCriticLookAhead(features_real, captions_real, a2c_network.policy_network,
                                                               a2c_network.value_network, most_likely=True)
            gen_cap_str = decode_captions(gen_cap, idx_to_word=test_data["idx_to_word"])
            real_cap_str = decode_captions(captions_real, idx_to_word=test_data["idx_to_word"])

            real_captions_file.write("\n".join(real_cap_str))
            generated_captions_file.write("\n".join(gen_cap_str))
            image_url_file.write("\n".join(urls))

            real_captions_file.flush()
            generated_captions_file.flush()
            image_url_file.flush()

            # a2c_network.value_network.valrnn.hidden_cell = repackage_hidden(a2c_network.value_network.valrnn.hidden_cell)
            a2c_network.value_network.valrnn.init_hidden()

        real_captions_file.close()
        generated_captions_file.close()
        image_url_file.close()
