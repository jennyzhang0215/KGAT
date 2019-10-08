import argparse
from dataset import DataLoader, L_DataLoader
from models import Model
import torch as th
import torch.optim as optim
import metric
from utils import creat_log_id, logging_config, MetricLogger
import numpy as np
from time import time
import os
import logging

def parse_args():
    parser = argparse.ArgumentParser(description="Reproduce KGAT using DGL")
    parser.add_argument('--gpu', type=int, default=0, help='use GPU')
    parser.add_argument('--seed', type=int, default=1234, help='the random seed')
    ### Data parameters
    parser.add_argument('--data_name', nargs='?', default='last-fm',  help='Choose a dataset from {yelp2018, last-fm, amazon-book}')
    #parser.add_argument('--adj_type', nargs='?', default='si', help='Specify the type of the adjacency (laplacian) matrix from {bi, si}.')
    ### Model parameters
    parser.add_argument('--entity_embed_dim', type=int, default=64, help='CF Embedding size.')
    parser.add_argument('--relation_embed_dim', type=int, default=64, help='CF Embedding size.')
    parser.add_argument('--gnn_num_layer', type=int, default=3, help='the number of layers')
    parser.add_argument('--gnn_hidden_size', type=int, default=64, help='Output sizes of every layer')
    parser.add_argument('--dropout_rate', type=float, default=0.1, help='Keep probability w.r.t. node dropout (i.e., 1-dropout_ratio) for each deep layer. 1: no dropout.')
    parser.add_argument('--regs', type=float, default=0.0001, help='Regularization for user and item embeddings.')

    ### Training parameters
    parser.add_argument('--max_epoch', type=int, default=10000, help='train xx iterations')
    parser.add_argument("--grad_norm", type=float, default=1.0, help="norm to clip gradient to")
    parser.add_argument('--lr', type=float, default=0.0001, help='Learning rate.')
    parser.add_argument('--batch_size', type=int, default=1024, help='CF batch size.')
    parser.add_argument('--batch_size_kg', type=int, default=2048, help='KG batch size.')
    parser.add_argument('--evaluate_every', type=int, default=1, help='the evaluation duration')
    parser.add_argument('--print_kg_every', type=int, default=500, help='the print duration of the kg part')
    parser.add_argument('--print_gnn_every', type=int, default=500, help='the print duration of the gnn part')
    #parser.add_argument("--eval_batch_size", type=int, default=-1, help="batch size when evaluating")
    args = parser.parse_args()
    save_dir = "{}_d{}_l{}_dp{}_lr{}_bz{}_kgbz{}_seed{}".format(args.data_name, args.entity_embed_dim,
                args.gnn_num_layer, args.dropout_rate, args.lr, args.batch_size, args.batch_size_kg, args.seed)
    args.save_dir = os.path.join('log', save_dir)
    if not os.path.isdir('log'):
        os.makedirs('log')
    if not os.path.isdir(args.save_dir):
        os.makedirs(args.save_dir)
    args.save_id = creat_log_id(args.save_dir)
    logging.info(args)
    return args


def train(args):
    logging_config(folder=args.save_dir, name='log{:d}'.format(args.save_id), no_console=False)

    ### check context
    use_cuda = args.gpu >= 0 and th.cuda.is_available()
    if use_cuda:
        th.cuda.set_device(args.gpu)

    ### load data
    dataset = L_DataLoader(args.data_name, num_neighbor_hop=args.gnn_num_layer, seed=args.seed)

    ### model
    model = Model(n_entities=dataset.num_all_entities, n_relations=dataset.num_all_relations,
                  entity_dim=args.entity_embed_dim, relation_dim=args.relation_embed_dim,
                  num_gnn_layers=args.gnn_num_layer, n_hidden=args.gnn_hidden_size, dropout=args.dropout_rate,
                  reg_lambda_kg=args.regs, reg_lambda_gnn=args.regs)
    if use_cuda:
        model.cuda()
    logging.info(model)
    ### optimizer
    optimizer = optim.Adam(model.parameters(), lr=args.lr)

    test_metric_logger = MetricLogger(['epoch', 'recall', 'ndcg'], ['%d', '%.5f', '%.5f'],
                                      os.path.join(args.save_dir, 'test{:d}.csv'.format(args.save_id)))
    best_epoch = -1
    best_recall = 0.0
    best_ndcg = 0.0
    model_state_file = 'model_state.pth'

    g, all_etype = dataset.generate_whole_g()
    nid_th = th.arange(dataset.num_all_entities, dtype=th.long)
    etype_th = th.LongTensor(all_etype)
    if use_cuda:
        nid_th, etype_th = nid_th.cuda(), etype_th.cuda()
    #print("nid_th", nid_th)
    #print("etype_th", etype_th)
    g.ndata['id'] = nid_th
    g.edata['type'] = etype_th
    if use_cuda:
        item_id_range = th.arange(dataset.num_items, dtype=th.long).cuda()
    else:
        item_id_range = th.arange(dataset.num_items, dtype=th.long)
    A_w = th.tensor(dataset.w)
    if use_cuda:
        A_w = A_w.cuda()
    print(A_w)
    g.edata['w'] = A_w

    for epoch in range(1, args.max_epoch+1):
        ### train GNN
        time1 = time()
        model.train()
        cf_sampler = dataset.CF_pair_sampler(batch_size=args.batch_size)
        iter = 0
        total_loss = 0.0
        for user_ids, item_pos_ids, item_neg_ids in cf_sampler:
            iter += 1
            user_ids_th = th.LongTensor(user_ids)
            item_pos_ids_th = th.LongTensor(item_pos_ids)
            item_neg_ids_th = th.LongTensor(item_neg_ids)
            if use_cuda:
                user_ids_th, item_pos_ids_th, item_neg_ids_th = \
                    user_ids_th.cuda(), item_pos_ids_th.cuda(), item_neg_ids_th.cuda()
            embedding = model.gnn(g)
            loss = model.get_loss(embedding, user_ids_th, item_pos_ids_th, item_neg_ids_th)
            loss.backward()
            # th.nn.utils.clip_grad_norm_(model.parameters(), args.grad_norm)  # clip gradients
            optimizer.step()
            optimizer.zero_grad()
            total_loss += loss.item()
            if (iter % args.print_gnn_every) == 0:
                logging.info("Epoch {:03d} Iter {:04d} | Loss {:.4f} ".format(epoch, iter, total_loss / iter))
        logging.info(['Time for GNN: {:.1f}s, loss {:.4f}'.format(time() - time1, total_loss / iter)])

        ### train kg
        time1 = time()
        kg_sampler = dataset.KG_sampler(batch_size=args.batch_size_kg)
        iter = 0
        total_loss = 0.0
        for h, r, pos_t, neg_t in kg_sampler:
            iter += 1
            model.train()
            h_th = th.LongTensor(h)
            r_th = th.LongTensor(r)
            pos_t_th = th.LongTensor(pos_t)
            neg_t_th = th.LongTensor(neg_t)
            if use_cuda:
                h_th, r_th, pos_t_th, neg_t_th = h_th.cuda(), r_th.cuda(), pos_t_th.cuda(), neg_t_th.cuda()
            loss = model.transR(h_th, r_th, pos_t_th, neg_t_th)
            # print("loss", loss)
            loss.backward()
            # print("start computing gradient ...")
            optimizer.step()
            optimizer.zero_grad()
            total_loss += loss.item()
            if (iter % args.print_kg_every) == 0:
               logging.info("Epoch {:03d} Iter {:04d} | Loss {:.4f} ".format(epoch, iter, total_loss/iter))
        logging.info(['Time for KGE: {:.1f}s, loss {:.4f}'.format(time() - time1, total_loss/iter)])


        with th.no_grad():
            A_w = model.compute_attention(g)
        g.edata['w'] = A_w
        print(A_w)
        if epoch % args.evaluate_every == 0:
            time1 = time()
            with th.no_grad():
                model.eval()
                all_embedding = model.gnn(g)
                recall, ndcg = metric.calc_recall_ndcg(all_embedding, dataset, item_id_range, K=20, use_cuda=use_cuda)

            # save best model
            if recall > best_recall:
                best_recall = recall
                best_ndcg = ndcg
                best_epoch = epoch
                #th.save({'state_dict': model.state_dict(), 'epoch': epoch}, model_state_file)
            logging.info(
                "[{:.1f}s]Epoch: {}, test recall:{:.5f}, ndcg:{:.5f}, best({}) recall:{:.5f}, ndcg:{:.5f}\n".format(
                    time() - time1, epoch, recall, ndcg, best_epoch, best_recall, best_ndcg))
            test_metric_logger.log(epoch=epoch, recall=recall, ndcg=ndcg)

if __name__ == '__main__':
    args = parse_args()
    train(args)