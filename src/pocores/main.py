#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
pocores.py

Originally written by Tobias Günther.

Copyright (C) 2011 Tobias Günther, Christian Dittrich.
Copyright (C) 2012 Jonathan Sonntag, Arne Neumann.
Copyright (C) 2014 Arne Neumann. (major rewrite using discoursegraphs library)

License: GNU Affero General Public License Version 3 (or later)
"""

import os
import codecs
import sys
import math
from collections import defaultdict, OrderedDict

import brewer2mpl
from unidecode import unidecode

import discoursegraphs as dg
from discoursegraphs.util import natural_sort_key, create_dir

from pocores import cli, filters
from pocores import preferences as prefs

# TODO: evaluate weights
WEIGHTS = [8, 2, 8, 3, 2, 7, 0]
MAX_SENT_DIST = 4


class Pocores(object):
    def __init__(self, document_graph):
        self.document = document_graph

        # maps from a token node ID (i.e. the first mention of an entity
        # in the text) to a list of token node IDs (i.e. all mentions of
        # that entity in the text, incl. the first one)
        self.entities = defaultdict(list)

        # maps from a mention (i.e. a token node ID) to an entity (i.e. a
        # token node ID that represents the first mention of that entity in the
        # text)
        self.mentions = {}

        # ana_to_ante maps from an anaphora token ID (str) to an
        # antecedent token ID (str)
        self.ana_to_ante = {}

        # EXPLETIVE_VERBS: a list of german expletive verbs
        self.EXPLETIVE_VERBS = {"sein", "regnen", "gelingen", "bestehen",
                                "geben"}

        # Class Variables # TODO: explain self.ref_id ???
        # TODO: I'm trying to get rid of ref_ids altogether, check if there
        #       are any issues
        #~
        #~ self.ref_id = 0 # Counter for Referents

        # contains the output of candidate filtering functions.
        # so far only used for debugging purposes
        self.filtered_results = OrderedDict()

        # entity grids (key: int sentence, value XXX
        # TODO: write entity grid description
        self.entity_grid = {}

        # stores lots of debug information for each coreference mention
        # candidate
        self.candidate_report = defaultdict(lambda : defaultdict(str))

    def node_attrs(self, token_node_id):
        """
        returns the attribute dictionary of a token, given its node ID.
        """
        return self.document.node[token_node_id]

    def _get_candidates(self):
        """
        Returns list of all known discourse entities.

        Returns
        -------
        candidates : list of str
            a sorted list of token node IDs
        """
        candidates = []
        for entity_node_id in self.entities:
            candidates += self.entities[entity_node_id]
        return sorted(candidates, key=natural_sort_key)

    def _get_children(self, token_node_id):
        """
        Given a token (node ID), returns this token and all its children in the
        dependency structure (list of token node IDs).

        Parameters
        ----------
        token_node_id : str
            ID of the toke node whose children will be fetched
        """
        return sorted(traverse_dependencies_down(self.document, token_node_id),
                      key=natural_sort_key)

    def _get_word(self, token_node_id):
        """
        returns the token (string) that a token node ID represents.

        TODO: rename to get_token()
        """
        return self.document.get_token(token_node_id)

    def _get_wordlist(self, token_node_ids, verbose=False):
        """
        Returns a list of tokens, either as a list of word strings or as a list
        of (token string, token node ID) tuples.
        """
        if not verbose:
            return (self._get_word(tni) for tni in token_node_ids)
        else:
            return ((self._get_word(tni), tni) for tni in token_node_ids)

    def _get_sentence(self, sent_id):
        """
        returns the sentence (string) that is referred to by a sentence ID

        Parameters
        ----------
        sent_id : int or str
            the node ID of a sentence (e.g. 's1') or the equivalent sentence
            index (e.g. 1)
        """
        assert isinstance(sent_id, (int, str))
        sid = sent_id if isinstance(sent_id, str) else 's{}'.format(sent_id)
        return dg.tokens2text(self.document, self.node_attrs(sid)['tokens'])

    def _get_coref_chains(self):
        """
        returns the coreference chain for each anaphora/entity.

        Returns
        -------
        coref_chains : list of lists of (str, str) tuples
            a list of coreference chain lists. each coreference chain is
            represented by an ordered list of (token string, token node ID)
            tuples
        """
        return [reversed(list(self._get_wordlist(self.entities[i], verbose=True)))
                for i in self.entities.keys()]

    def print_entity_grid(self, min_coref_chain_length=2,
                          deprel_attr=None):
        """
        prints all coreference chains, including the grammatical function of
        anaphora and potential antecedents.
        """
        if deprel_attr is None:
            deprel_attr = self.document.deprel_attr

        for coref_chain in self._get_coref_chains():
            coref_chain = list(coref_chain)
            if len(coref_chain) >= min_coref_chain_length:
                # node ID of the first token in the chain
                print "\n\nEntity '{0}':".format(coref_chain[0][1])
                for (token, node_id) in coref_chain:
                    token_dict = self.node_attrs(node_id)
                    sent_index = token_dict['sent_pos']
                    deprel = token_dict[deprel_attr]
                    print (u"\t{0} in sentence {1} with function "
                           u"'{2}'".format(token, sent_index, deprel))

    def _get_entity_grid(self, min_coref_chain_length=2,
                         deprel_attr=None):
        """
        returns the entity grid as a dictionary.

        TODO: describe the entity grid
        """
        if deprel_attr is None:
            deprel_attr = self.document.deprel_attr

        coref_chains = [chain for chain in self._get_coref_chains()
                        if len(chain) >= min_coref_chain_length]
        for sent_id in self.document.sentences:
            self.entity_grid[sent_id] = defaultdict(list)

        for chain_index, coref_chain in enumerate(coref_chains):
            for (token, token_id) in coref_chain:
                token_dict = self.node_attrs(token_id)
                sent_index = token_dict['sent_pos']
                deprel = token_dict[deprel_attr]
                self.entity_grid[sent_index][chain_index].append(deprel)
        return self.entity_grid, coref_chains

    def resolve_anaphora(self, weights=WEIGHTS, max_sent_dist=4, pos_attr=None,
                         deprel_attr=None, debug=False):
        """
        Resolves all nominal and pronominal anaphora in the text (stored in the
        classes sentence dictionary).
        Takes a list of weights as argument, which is passed on to the function
        for pronominal resolution.

        Parameters
        ----------
        weights : list of int
            list of 7 weights that will be used for ranking anaphora candidates
        max_sent_dist: int
            number of preceding sentences that will be looked at, i.e. the
            sentences that contain potential antecedents

        TODO: make noun_tags, pronoun_tags set constants for effienciency
        TODO: convert weights into a namedtuple
        """
        if deprel_attr is None:
            deprel_attr = self.document.deprel_attr
        if pos_attr is None:
            pos_attr = self.document.pos_attr

        assert isinstance(weights, list), \
            'Weights should be a list, not a "{0}"'.format(type(weights))
        assert all([isinstance(weight, int) for weight in weights]), \
            'All weights should be integers, got "{0}" instead'.format(weights)
        assert len(weights) == 7, \
            'There should be 7 weights, not {0}'.format(len(weights))

        # TODO: explain/show, why these structures have to be reset
        self.entities.clear()
        self.ana_to_ante.clear()
        self.candidate_report.clear()

        noun_tags = ("NN", "NE")
        pronoun_tags = ("PPER", "PRELS", "PRF", "PPOSAT", "PDS")

        for sent_id in self.document.sentences:
            for token_id in self.node_attrs(sent_id)['tokens']:
                tok_attrs = self.node_attrs(token_id)
                # Treatment of Nominals
                if (tok_attrs[pos_attr] in noun_tags
                   and tok_attrs[deprel_attr] != "PNC"):
                    self._resolve_nominal_anaphora(token_id, max_sent_dist,
                                                   debug=debug)

                # Treatment of Pronominals
                elif (tok_attrs[pos_attr] in pronoun_tags
                      and not filters.is_expletive(self, token_id)):
                    self._resolve_pronominal_anaphora(token_id, weights,
                                                      max_sent_dist,
                                                      debug=debug)

    def _resolve_nominal_anaphora(self, anaphora, max_sent_dist, debug=False):
        """
        Tries to resolve a given nominal anaphora.
        If this fails the given word is registered as a new discourse entity.

        Parameters
        ----------
        anaphora : str
            the token ID of the anaphora
        max_sent_dist : int
            look for potential antecedents only in the preceding
            `max_sent_dist` number of sentences
        debug : bool
            produce additional debugging output

        Returns
        -------
        anaphora_or_antecedent : str
            Returns the token node ID of the antecedent, iff an antecedent
            was found. Otherwise the input (i.e. the token node ID of the
            anaphora) is returned.
        """
        self.candidate_report[anaphora]['anaphora_type'] = 'nominal'

        cand_list = self._get_candidates()
        filtered_candidates = filters.get_filtered_candidates(
            self, cand_list, anaphora, max_sent_dist, verbose=debug)

        # iterate over antecedent candidates, starting from the closest
        # preceding one to the left-most one
        filtered_candidates.reverse()
        for antecedent in filtered_candidates:
            if filters.is_coreferent(self, antecedent, anaphora):
                first_mention = self.mentions[antecedent]
                self.entities[first_mention].append(anaphora)
                self.mentions[anaphora] = first_mention
                return first_mention

        # 'anaphora' represents an entity that wasn't mentioned before
        if anaphora not in self.mentions:
            self.entities[anaphora] = [anaphora]
            self.mentions[anaphora] = anaphora
            return anaphora

    def _resolve_pronominal_anaphora(self, anaphora, weights, max_sent_dist,
                                     pos_attr=None, debug=False):
        """
        Tries to resolve a given pronominal anaphora by applying different
        filters and preferences.
        For the weighting of the different preferences a list of weights has to
        be passed to the function. If resolution fails, the given pronoun is
        registered as a new discourse entity.

        This method relies on these grammatical categories:

            - PRELS: substitutive relative pronoun, e.g. [der Hund ,] der
            - PDS: substitutive demonstrative pronoun, e.g. dieser, jener

            - PPER: irreflexive personal pronoun, e.g. ich, er, ihm, mich, dir
            - PRF: reflexive personal pronoun, e.g. sich, einander, dich, mir
            - PPOSAT: attributive possesive pronoung, e.g. mein [Buch],
              deine [Mutter]

        Parameters
        ----------
        anaphora : str
            the token ID of the anaphora
        weights: list of int
            the 7 weights used for ranking antecedent candidates
        max_sent_dist : int
            look for potential antecedents only in the preceding
            `max_sent_dist` number of sentences
        pos_attr : str or None
            the name of the POS column (e.g. 'ppos') from the CoNLL input file
            that will be used (there are often two of them)
        debug : bool
            produce additional debugging output

        TODO: provide documentation for scoring and/or convert weights into
              a namedtuple
        """
        if pos_attr is None:
            pos_attr = self.document.pos_attr

        report = self.candidate_report[anaphora]
        report['anaphora_type'] = 'pronominal'

        cand_list = self._get_candidates()
        filtered_candidates = filters.get_filtered_candidates(self, cand_list,
                                                              anaphora,
                                                              max_sent_dist,
                                                              verbose=debug)

        report['candidates'] = cand_list
        report['filtered_candidates'] = filtered_candidates

        if not filtered_candidates:
            self.entities[anaphora] = [anaphora]
            self.mentions[anaphora] = anaphora
            return anaphora

        # Preferences
        can_dict = dict.fromkeys(filtered_candidates, 0)
        anaphora_pos = self.node_attrs(anaphora)[pos_attr]
        if anaphora_pos in set(["PRELS", "PDS"]):
            # chooses the most recent candidate, if the word is a substitutive
            # demonstrative/relative pronoun
            antecedent = max(can_dict)

        elif anaphora_pos in set(["PPER", "PRF", "PPOSAT"]):
            # scores the candidates, if the word is a personal pronoun or an
            # attributive possessive pronoun
            for can in can_dict:
                if prefs.check_parallelism(self, can, anaphora):
                    can_dict[can] += weights[0]
                if prefs.check_role(self, can, "SB"):
                    can_dict[can] += weights[1]
                if prefs.check_role(self, can, "OA"):
                    can_dict[can] += weights[2]
                if prefs.check_role(self, can, "DA"):
                    can_dict[can] += weights[3]
                can_dict[can] += \
                    weights[4] * math.log(prefs.get_chain_length(self, can))
                can_dict[can] -= weights[5] * filters.distance(can, anaphora)
                can_dict[can] -= weights[6] * prefs.get_depth(self, can)
                # NOTE: additional preferences can be added here

            # Pick candidate with highest Score. If there are candidates with
            # the same score, pick closest
            # TODO: debug this after _get_candidates() works
            antecedent = sorted([(v, k)
                                for k, v in can_dict.iteritems()],
                                reverse=True)[0][1]

        # TODO: add other pronoun resolution algorithm
        # if anaphora_pos in [OTHER PRONOUNS]:
            # antecedent = Result of OTHER PRONOUN RESOLUTION

        # Store Result
        self.ana_to_ante[anaphora] = antecedent  # for Evaluation
        first_mention = self.mentions[antecedent]
        self.entities[first_mention].append(anaphora)
        self.mentions[anaphora] = first_mention
        return first_mention

    def add_coreference_chains_to_docgraph(self):
        for chain_generator in self._get_coref_chains():
            chain = list(chain_generator)
            chain_len = len(chain)
            if chain and chain_len > 1:
                for i, (token, token_node_id) in enumerate(chain):
                    if i < chain_len-1:  # if it's not the last/only element
                        _ant_tok, ant_node_id = chain[i+1]

                        markable_attrs = {'pocores:type': 'anaphoric',
                                          'pocores:anaphor_antecedent': ant_node_id,
                                          'pocores:referentiality': 'referring'}

                        edge_attrs = {'edge_type': dg.EdgeTypes.pointing_relation,
                                      'label': 'pocores:antecedent'}
                        layers = {'pocores', 'pocores:markable'}
                        self.document.add_edge(token_node_id, ant_node_id, layers, attr_dict=edge_attrs)

                    else:
                        markable_attrs = {'pocores:type': 'none',
                                          'pocores:anaphor_antecedent': 'empty',
                                          'pocores:referentiality': 'discourse-new'}

                    self.document.node[token_node_id]['layers'].update({'pocores:markable'})
                    self.document.node[token_node_id].update(markable_attrs)


def traverse_dependencies_down(docgraph, node_id):
    """
    TODO: convert docgraph from multidigraph into digraph to avoid having
    to iterate over a single edge_id.
    """
    yield node_id
    out_edges = docgraph.edge[node_id]
    for target in out_edges:
        if any(edge_attr['edge_type'] == dg.EdgeTypes.dominance_relation
               for edge_id, edge_attr in out_edges[target].iteritems()):
            for target_id in traverse_dependencies_down(docgraph, target):
                yield target_id


def output_with_brackets(pocores):
    """
    Returns the input text annotated with the resolved discourse referents
    (in brackets).

    Example sentence:
    Als [die übrigen Personen]_{154} mit Bediensteten der Justiz eintreten ,
    um [Don Giovanni]_{1} zu verhaften , erzählt [ihnen]_{154}
    [Leporello , was geschehen ist]_{4} .
    """
    return_str = u""

    for sent_id in pocores.document.sentences:
        # collect brackets
        opening = {}  # ( wordid:[ref_id, ref_id, ...] )
        closing = {}
        token_ids = pocores.node_attrs(sent_id)['tokens']
        for token_id in token_ids:
            first_mention = pocores.mentions.get(token_id)
            if first_mention and len(pocores.entities[first_mention]) > 1:
                children = pocores._get_children(token_id)
                first_token = mintok(children)
                if first_token not in opening:
                    opening[first_token] = [first_mention]
                else:
                    opening[first_token].insert(0, first_mention)
                last_token = maxtok(children)
                if last_token not in closing:
                    closing[last_token] = [first_mention]
                else:
                    closing[last_token].insert(0, first_mention)

        sent_str = ""
        for token_id in token_ids:  # iterate over tokens in sentence
            if token_id in opening:
                for mention_id in opening[token_id]:
                    sent_str += "["
            sent_str += pocores.node_attrs(token_id)['token']
            if token_id in closing:
                for mention_id in closing[token_id]:
                    sent_str += "]_{" + mention_id + "}"
            sent_str += " "
        return_str += sent_str + "\n"
    return return_str


def brat_output(pocores):
    ret_str = u''
    cid = 1
    candidates_to_cid = {}
    for can in pocores._get_candidates():
        candidates_to_cid[can] = cid
        cid += 1

    onset = 0
    for i, token_id in enumerate(pocores.document.tokens):
        tok_len = len(pocores._get_word(token_id))
        if token_id in pocores.mentions:
            entity_str = \
                unidecode(pocores._get_word(pocores.mentions[token_id]))
            ret_str += u"T{}\t{} {} {}\t{}\n".format(
                candidates_to_cid[token_id], entity_str, onset, onset+tok_len,
                pocores._get_word(token_id))
        onset += tok_len+1
    return ret_str


def create_annotation_conf(pocores):
    """
    creates a brat annotation.conf file (as a string)
    for the given pocores instance.
    """
    ret_str = '[entities]\n\n'
    unique_entities = {pocores._get_word(eid) for eid in pocores.entities}
    for entity in unique_entities:
        ret_str += unidecode(entity) + '\n'
    ret_str += '\n[relations]\n\n[events]\n\n[attributes]'
    return ret_str


def create_visual_conf(pocores):
    """
    creates a visual.conf file (as a string)
    for the given pocores instance.
    """
    ret_str = u'[drawing]\n\n'
    num_of_entities = len(pocores.entities)
    mapsize = min(num_of_entities, 12)
    colormap = brewer2mpl.get_map('Paired', 'Qualitative', mapsize)
    colors = range(mapsize) * int(math.ceil(num_of_entities / float(mapsize)))

    for i, entity_id in enumerate(sorted(pocores.entities,
                                         key=natural_sort_key)):
        # TODO: check why there are empty entities at all
        if pocores.entities[entity_id]:
            background_color = colormap.hex_colors[colors[i]]
            ascii_entity = unidecode(pocores._get_word(entity_id))
            ret_str += u'{}\tbgColor:{}\n'.format(ascii_entity,
                                                  background_color)
    ret_str += '\n[labels]'
    return ret_str


def write_brat(pocores, output_dir):
    create_dir(output_dir)
    doc_name = os.path.basename(pocores.document.name)
    with codecs.open(os.path.join(output_dir, doc_name+'.txt'),
                     'wb', encoding='utf-8') as txtfile:
        txtfile.write(dg.get_text(pocores.document))
    with codecs.open(os.path.join(output_dir, 'annotation.conf'),
                     'wb', encoding='utf-8') as annotation_conf:
        annotation_conf.write(create_annotation_conf(pocores))
    with codecs.open(os.path.join(output_dir, 'visual.conf'),
                     'wb', encoding='utf-8') as visual_conf:
        visual_conf.write(create_visual_conf(pocores))
    with codecs.open(os.path.join(output_dir, doc_name+'.ann'),
                     'wb', encoding='utf-8') as annfile:
        annfile.write(brat_output(pocores))


def mintok(token_ids):
    """
    given a list of token node IDs, returns the token node ID of the token that
    occurs first in the text.
    """
    return min(token_ids, key=natural_sort_key)


def maxtok(token_ids):
    """
    given a list of token node IDs, returns the token node ID of the token that
    occurs last in the text.
    """
    return max(token_ids, key=natural_sort_key)


def print_coreference_report(pocores):
    non_trivial_chains = []
    singletons = []
    for chain_generator in pocores._get_coref_chains():
        chain = list(chain_generator)
        if chain:
            if len(chain) > 1:
                non_trivial_chains.append(chain)
            else:
                singletons.append(chain)

    print "\nSingletons:\n"
    for singleton in singletons:
        print singleton

    print "\nCoreference chains:\n"
    for chain in non_trivial_chains:
        print chain

def make_xml(pocores):  
    from lxml import etree
    import datetime as _dt
    
    def idx_string_to_int(string_idx, pt=False):
	sentence_id, word_id = string_idx.split('_')
	if pt:
	    return int( sentence_id[1:] )
	return int( word_id[1:] )
    
    root = etree.Element('root')
    if len(str(_dt.datetime.now().minute)) == 1:
        dt = str(_dt.datetime.now().year) + '/' + str(_dt.datetime.now().month) + '/' + str(_dt.datetime.now().day) + ' ' + str(_dt.datetime.now().hour) + ':0' + str(_dt.datetime.now().minute)
    else:
        dt = str(_dt.datetime.now().year) + '/' + str(_dt.datetime.now().month) + '/' + str(_dt.datetime.now().day) + ' ' + str(_dt.datetime.now().hour) + ':' + str(_dt.datetime.now().minute)
    document = etree.Element('document', computation_date=dt)
    
    mention_sets = {}
    text = etree.Element('text')
    sentence_idx = 1
    for sent_id in pocores.document.sentences:
        sentence_element = etree.Element('sentence', index=str(sentence_idx))
        
        token_index = 1
        for token_id in pocores.node_attrs(sent_id)['tokens']:
            if 'conll:number' in pocores.node_attrs(token_id) and 'conll:person' in pocores.node_attrs(token_id) and 'conll:gender' in pocores.node_attrs(token_id):
                if "NE" in pocores.node_attrs(token_id):
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'], 
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                num = pocores.node_attrs(token_id)['conll:number'],
                                                                pers = str(pocores.node_attrs(token_id)['conll:person']),
                                                                gen = pocores.node_attrs(token_id)['conll:gender'],
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                # TODO: what was that?
                                                                ne = pocores.node_attrs(token_id)["NE"],
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"]
                                                    )
                else:
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'], 
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                num = pocores.node_attrs(token_id)['conll:number'],
                                                                pers = str(pocores.node_attrs(token_id)['conll:person']),
                                                                gen = pocores.node_attrs(token_id)['conll:gender'],
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"]
                                                    )
            elif "conll:person" in pocores.node_attrs(token_id) and "conll:gender" in pocores.node_attrs(token_id):
                if "NE" in pocores.node_attrs(token_id):
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'], 
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                pers = str(pocores.node_attrs(token_id)['conll:person']),
                                                                gen = pocores.node_attrs(token_id)['conll:gender'],
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"],
                                                                ne = pocores.node_attrs(token_id)["NE"]
                                                    )
                else:
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                pers = str(pocores.node_attrs(token_id)['conll:person']),
                                                                gen = pocores.node_attrs(token_id)['conll:gender'],
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"]
                                                    )
            elif "conll:number" in pocores.node_attrs(token_id) and "conll:person" in pocores.node_attrs(token_id):
                if "NE" in pocores.node_attrs(token_id):
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                num = pocores.node_attrs(token_id)['conll:number'],
                                                                pers = str(pocores.node_attrs(token_id)['conll:person']),
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"],
                                                                ne = pocores.node_attrs(token_id)["NE"]
                                                    )
                else:
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                num = pocores.node_attrs(token_id)['conll:number'],
                                                                pers = str(pocores.node_attrs(token_id)['conll:person']),
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"]
                                                    )
            elif "conll:number" in pocores.node_attrs(token_id) and "conll:gender" in pocores.node_attrs(token_id):
                if "NE" in pocores.node_attrs(token_id):
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                num = pocores.node_attrs(token_id)['conll:number'],
                                                                gen = pocores.node_attrs(token_id)['conll:gender'],
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"],
                                                                ne = pocores.node_attrs(token_id)["NE"]
                                                    )
                else:
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                num = pocores.node_attrs(token_id)['conll:number'],
                                                                gen = pocores.node_attrs(token_id)['conll:gender'],
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"]
                                                    )
            elif "conll:person" in pocores.node_attrs(token_id):
                if "NE" in pocores.node_attrs(token_id):
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                pers = str(pocores.node_attrs(token_id)['conll:person']),
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"],
                                                                ne = pocores.node_attrs(token_id)["NE"]
                                                    )
                else:
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                pers = str(pocores.node_attrs(token_id)['conll:person']),
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"]
                                                    )
            elif "conll:number" in pocores.node_attrs(token_id):
                if "NE" in pocores.node_attrs(token_id):
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                num = pocores.node_attrs(token_id)['conll:number'],
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"],
                                                                ne = pocores.node_attrs(token_id)["NE"]
                                                    )
                else:
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                num = pocores.node_attrs(token_id)['conll:number'],
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"]
                                                    )
            elif "conll:gender" in pocores.node_attrs(token_id):
                if "NE" in pocores.node_attrs(token_id):
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                gen = pocores.node_attrs(token_id)['conll:gender'],
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"],
                                                                ne = pocores.node_attrs(token_id)["NE"]
                                                    )
                else:
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                gen = pocores.node_attrs(token_id)['conll:gender'],
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"]
                                                    )
            else:
                if "NE" in pocores.node_attrs(token_id):
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"],
                                                                ne = pocores.node_attrs(token_id)["NE"]
                                                    )
                else:
                    token_element = etree.Element   ('token',   index=pocores.node_attrs(token_id)['word_id'],
                                                                form = pocores.node_attrs(token_id)['token'],
                                                                lemma = pocores.node_attrs(token_id)['plemma'],
                                                                pos = pocores.node_attrs(token_id)['ppos'],
                                                                head = str(pocores.node_attrs(token_id)["phead"]),
                                                                deprel = pocores.node_attrs(token_id)["pdeprel"]
                                                    )
            
            token_index += 1
                                    
            sentence_element.append(token_element)
        
        text.append(sentence_element)
        sentence_idx += 1
    
    mentions_element = etree.Element('mentions')
    for head_ana in pocores.entities:
	mention_set_element = etree.Element('coreferring_set', id=str(head_ana))
	mention_ids = pocores.entities[head_ana]
	for mention_id in mention_ids:
	    kids = pocores._get_children(mention_id)
	    this_surface_form = [ ]
	    for entry in kids:
		this_surface_form.append(pocores.node_attrs(entry)['token'])
	    
	    mention_element = etree.Element('mention', sentence_id=str(idx_string_to_int(mention_id, True)), start_token=str( idx_string_to_int(kids[0]) ), end_token=str( idx_string_to_int(kids[-1]) ), surface_form=(' '.join(this_surface_form)) )
	    mention_set_element.append(mention_element)
	mentions_element.append(mention_set_element)
    
    document.append(text)
    document.append(mentions_element)
    root.append(document)
    return (etree.tostring(root, xml_declaration=True, pretty_print=True, encoding='utf8'))



def run_pocores(input_file, input_format, output_dest=None,
                output_format='bracketed', weights=WEIGHTS,
                max_sent_dist=MAX_SENT_DIST, debug=False,
                eval_file=None):
    """
    run the pocores coreference system on a mate-parsed, CoNLL-formatted
    input file.
    """
    assert input_format in ('2009', '2010')
    assert output_format in ('bracketed', 'brat', 'xml')

    if input_format == '2009':
        docgraph = dg.read_conll(input_file, conll_format=input_format,
                                      deprel_attr='pdeprel', feat_attr='pfeat',
                                      head_attr='phead', lemma_attr='plemma',
                                      pos_attr='ppos')
    else:  # conll 2010 format
        docgraph = dg.read_conll(input_file, conll_format=input_format,
                                      deprel_attr='pdeprel', feat_attr='pfeat',
                                      head_attr='phead', lemma_attr='lemma',
                                      pos_attr='ppos')

    pocores = Pocores(docgraph)
    pocores.resolve_anaphora(weights, max_sent_dist, debug=debug)
    pocores.add_coreference_chains_to_docgraph()

    if output_format == 'bracketed':
        if isinstance(output_dest, file):
            output_dest.write(output_with_brackets(pocores))
        else:
            path_to_dir, _filename = os.path.split(output_dest)
            create_dir(path_to_dir)
            with codecs.open(output_dest, 'w', 'utf-8') as output_file:
                output_file.write(output_with_brackets(pocores))
    elif output_format == 'xml':
	if isinstance(output_dest, file):
            output_dest.write(make_xml(pocores))
        else:
            path_to_dir, _filename = os.path.split(output_dest)
            create_dir(path_to_dir)
            with codecs.open(output_dest, 'w', 'utf-8') as output_file:
                output_file.write(make_xml(pocores))
    else:  # 'brat'
        if not isinstance(output_dest, file):
            # output_dest will be treated as a directory
            write_brat(pocores, output_dest)
        else:
            sys.stderr.write('For brat output specify an output folder.\n')
            sys.exit(1)

    if debug:
        print_coreference_report(pocores)

    if eval_file:
        # TODO: implement proper scorer.pl-based evaluation
        # there's some useful code in the /var/local/git/Depot/coreference.git
        # repo on hebe
        raise NotImplementedError

    return pocores


def run_pocores_with_cli_arguments(argv=sys.argv[1:]):
    """
    Run the pocores coreference system with the given command line arguments.
    This will generate output files in brat or bracket format.

    Parameters
    ----------
    argv : list of str
        a list of command line arguments (usually from sys.argv[1:])
    """
    parser, args = cli.parse_options(argv)
    if args.input is None:
        parser.print_help()
        sys.exit(0)

    weights = WEIGHTS
    if args.weights:  # if set, use command line weights.
        weight_str_list = args.weights.split(',')
        try:
            weights = [int(weight) for weight in weight_str_list]
        except ValueError as e:
            print "Can't convert all weights to integers. {0}".format(e)

    max_sent_dist = MAX_SENT_DIST
    if args.max_sent_dist:  # if set, use sentence distance set via cli
        try:
            max_sent_dist = int(args.max_sent_dist)
        except ValueError as e:
            print "max_sent_dist must be an integer. {0}".format(e)

    run_pocores(input_file=args.input, input_format=args.informat,
                output_dest=args.output_dest,
                output_format=args.outformat, weights=weights,
                max_sent_dist=max_sent_dist, debug=args.debug,
                eval_file=args.eval_file)

if __name__ == '__main__':
    """
    parses command line arguments, runs coreference analysis and produdes
    output (stdout or file(s)).
    """
    run_pocores_with_cli_arguments(sys.argv[1:])
