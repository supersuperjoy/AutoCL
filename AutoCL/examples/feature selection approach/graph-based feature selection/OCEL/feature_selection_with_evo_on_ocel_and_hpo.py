import json
import os
import time
from random import shuffle
from owlready2 import get_ontology, destroy_entity
import logging
import numpy as np
import pandas as pd

from search import calc_prediction
from ontolearn.knowledge_base import KnowledgeBase
from ontolearn.learning_problem import PosNegLPStandard
from owlapy.model import OWLNamedIndividual, IRI
from ontolearn.concept_learner import EvoLearner
from owlapy.render import DLSyntaxObjectRenderer
from optuna_random_sampler_ocel import OptunaSamplers
from wrapper_ocel import ocelWrapper


DIRECTORY = './FsEvoHpoOCEL/'
LOG_FILE = 'featureSelectionWithEvolearnerAndHPOOnOCEL.log'
DATASET = 'mammograph'
LOG_FILE_PATH = os.path.join(DIRECTORY, LOG_FILE)

logging.basicConfig(filename=LOG_FILE_PATH,
                    filemode="a",
                    format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.INFO)
    
try:
    os.chdir("Ontolearn/examples")
except FileNotFoundError:
    logging.error(FileNotFoundError)
    pass

df = pd.DataFrame(columns=['LP', 'max_runtime', 
                               'max_num_of_concepts_tested', 'iter_bound','quality_func',
                               'quality_score', 'Validation_f1_Score',
                               'Validation_accuracy'])

path_dataset = f'AutoCL/examples/dataset/{DATASET}.json'

with open(path_dataset) as json_file:
    settings = json.load(json_file)
if not os.path.exists(DIRECTORY):
    os.makedirs(DIRECTORY)


def get_data_properties(onto):
    try:
        data_properties = onto.data_properties()
    except Exception as e:
        data_properties = None
        logging.error(e)
    return data_properties


def get_object_properties(onto):
    try:
        object_properties = onto.object_properties()
    except Exception as e:
        object_properties = None
        logging.error(e)
    return object_properties


def create_new_kb_without_features(features):
    prop_object = list(get_object_properties(onto)) + list(get_data_properties(onto))
    for prop in prop_object:
        if prop.name not in features:
            destroy_entity(prop)
    onto.save("newkb.owl")
    return KnowledgeBase(path="./newkb.owl")


def get_prominent_properties_occurring_in_top_k_hypothesis(lp, knowledgebase, target_concepts, n=10):
    model = EvoLearner(knowledge_base=knowledgebase)
    model.fit(lp)
    model.save_best_hypothesis(n=n, path=f'Predictions_{target_concepts}')
    hypotheses = list(model.best_hypotheses(n=10))
    prop_object = list(get_object_properties(onto)) + list(get_data_properties(onto))
    print('Prop_Object', prop_object)

    dlr = DLSyntaxObjectRenderer()
    concept_sorted = [dlr.render(c.concept) for c in hypotheses]

    properties = []
    with open(f'{DIRECTORY}{DATASET}_featureSelectionWithEvolearnerAndHPO.txt', 'a') as f:
        print('Top 10 hypotheses:', file=f)
        for concepts in concept_sorted:
            print("Concepts", concepts, file=f)
            data_object_properties_in_concepts = [prop.name for prop in prop_object if prop.name in concepts]
            properties.extend(data_object_properties_in_concepts)
    properties_from_concepts = list(set(properties))
    return properties_from_concepts


if __name__ == "__main__":
    onto = get_ontology(settings['data_path']).load()
    kb = KnowledgeBase(path=settings['data_path'])    

    for str_target_concept, examples in settings['problems'].items():
        p = set(examples['positive_examples'])
        n = set(examples['negative_examples'])

        typed_pos = list(set(map(OWLNamedIndividual, map(IRI.create, p))))
        typed_neg = list(set(map(OWLNamedIndividual, map(IRI.create, n))))

        # shuffle the Positive and Negative Sample
        shuffle(typed_pos)
        shuffle(typed_neg)

        # Split the data into Training Set, Validation Set and Test Set
        train_pos, val_pos, test_pos = np.split(typed_pos,
                                                [int(len(typed_pos)*0.6),
                                                 int(len(typed_pos)*0.8)])
        train_neg, val_neg, test_neg = np.split(typed_neg,
                                                [int(len(typed_neg)*0.6),
                                                 int(len(typed_neg)*0.8)])
        train_pos, train_neg = set(train_pos), set(train_neg)
        val_pos, val_neg = set(val_pos), set(val_neg)
        test_pos, test_neg = set(test_pos), set(test_neg)

        lp = PosNegLPStandard(pos=train_pos, neg=train_neg)

        st = time.time()
        relevant_prop = get_prominent_properties_occurring_in_top_k_hypothesis(lp, kb, str_target_concept, n=10)
        new_kb = create_new_kb_without_features(relevant_prop)
        print('Converted to KB')

        # Optimise new_kb and get the hpo result
        optuna1 = OptunaSamplers(DATASET, new_kb, lp, str_target_concept, val_pos, val_neg, df)
        optuna1.get_best_optimization_result_for_cmes_sampler(100)
        # optuna1.convert_to_csv()
        print('HPO DONE')

        # get the best hpo
        best_hpo = df.loc[df['Validation_f1_Score'] == df['Validation_f1_Score'].values.max()]
        if len(best_hpo.index) > 1:
            best_hpo = best_hpo.loc[(best_hpo['Validation_accuracy'] == best_hpo['Validation_accuracy'].values.max()) &
                                    (best_hpo['max_runtime'] == best_hpo['max_runtime'].values.min())]
        logging.info(f"BEST HPO : {best_hpo}")

        st1 = time.time()
        # Get the new concepts generated after fs using new_kb and hpo
        wrap_obj = ocelWrapper(knowledge_base=new_kb,
                                max_runtime=int(best_hpo['max_runtime'].values[0]),
                                max_num_of_concepts_tested=int(best_hpo['max_num_of_concepts_tested'].values[0]),
                                iter_bound=int(best_hpo['iter_bound'].values[0]),
                                quality_func=str(best_hpo['quality_func'].values[0]),
                                )
        model = wrap_obj.get_ocel_model()
        model.fit(lp, verbose=False)
        model.save_best_hypothesis(n=3, path='Predictions_{0}'.
                                   format(str_target_concept))
        hypotheses = list(model.best_hypotheses(n=1))
        hypotheses = [hypo for hypo in hypotheses]
        print(hypotheses)
        predictions = model.predict(individuals=list(test_pos | test_neg),
                                    hypotheses=hypotheses)
        f1_score, accuracy = calc_prediction(predictions, test_pos, test_neg)
        quality = hypotheses[0].quality
        et = time.time()
        elapsed_time = et - st
        elapsed_time_1 = et - st1
        with open(f'{DIRECTORY}{DATASET}_featureSelectionWithEvolearnerAndHPO.txt', 'a') as f:
            print('Learning Problem:', str_target_concept, file=f)
            print("Concept Generated After Feature Selection:", hypotheses[0])
            print('F1 Score:', f1_score[1], file=f)
            print('Accuracy:', accuracy[1], file=f)
            print('Time Taken:', elapsed_time, file=f)
            print('best hpo', best_hpo, file=f)
            print('best_hpo-maxr', best_hpo['max_runtime'].values[0], file=f)
            print('best_hpo-max_num_of_concepts_tested', best_hpo['max_num_of_concepts_tested'].values[0], file=f)
            print('best_hpo-iter_bound', best_hpo['iter_bound'].values[0], file=f)
            print('best_hpo-quality_func', best_hpo['quality_func'].values[0], file=f)
            print('Time Taken 2:', elapsed_time_1, file=f)
            print('----------------------------', file=f)