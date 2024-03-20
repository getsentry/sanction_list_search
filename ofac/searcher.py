#!/usr/bin/env python3

import fuzzy
from timeit import default_timer as timer
from collections import Counter

from reader import load_sanctions
from dataobjects import NamePart
from dataobjects import NameAlias
import sdn as parser

dmeta = fuzzy.DMetaphone()

import normalizer

def find_noise_words(id_to_name):
    """
    Finds the most common words in the corpus. Use them as stopwords. Uses a higher percentage for stopwords from especially short words.
    TODO should be a static, human-verified list, based on both relevant input names (customer lists) and all of the sanction lists
    """
    words = []
    short_words = []
    for reference, list_subject in id_to_name.items():
        (aliases, birthdates) = list_subject
        name_parts = normalizer.normalize_aliases(aliases)
        for name_part in name_parts:
            if len(name_part) < 2:
                continue
            elif len(name_part) <= 4:
                short_words.append(name_part)
            else:
                words.append(name_part)

    different_words = set(words)
    different_shortwords = set(short_words)
    stopword_count = int(1.5 * len(words) / len(different_words))  # heuristic
    stopword_count_short_words = int(2 * len(short_words) / len(different_shortwords))  # heuristic

    word_counter = Counter()
    short_word_counter = Counter()

    word_counter.update(list(words))
    short_word_counter.update(list(short_words))
    stop_words = set([word[0] for word in word_counter.most_common(stopword_count)])
    stop_words_short = set([word[0] for word in short_word_counter.most_common(stopword_count_short_words)])
    return stop_words.union(stop_words_short)


def compute_phonetic_bin_lookup_table(id_to_name, stop_words):
    """
        Computation of hashmap of phonetic bin to list of list-entries
    """
    bin_to_id = {}
    for reference, list_subject in id_to_name.items():
        (aliases, birthdates) = list_subject
        unique_name_parts = set(normalizer.normalize_aliases(aliases))
        for name_part in unique_name_parts:
            if len(name_part) < 2 or name_part in stop_words:
                # skip stop words and words of one character only. TODO consider including stopwords, but penalise matches by stopword only
                continue

            try:
                bins = [b for b in dmeta(name_part) if b]  # dmeta sometimes outputs an empty 'None' bin, filter it out
            except UnicodeEncodeError:
                continue  # Ignores non-latin words silently. That's ok when input is latin alphabet only.

            for bin in bins:
                if not bin in bin_to_id:  # if bin not already added to dictionary
                    bin_to_id[bin] = []  # begin a new list of references

                bin_to_id[bin].append((reference, name_part))

    max_count = len(id_to_name) / 8  # if 100%/8 = 12.5% or more of the entries has it
    filtered_dict = remove_outliers(bin_to_id, max_count)

    return filtered_dict


def remove_outliers(bin_to_id, max_count):
    outliers = []
    for bin, references in bin_to_id.items():
        if len(references) > max_count:  # number of elements in the hashbin is greater than
            # the number of subjects in total
            outliers.append(bin)
    filtered_dict = {key: bin_to_id[key] for key in bin_to_id if key not in outliers}
    return filtered_dict


from fuzzywuzzy import fuzz
from Levenshtein import StringMatcher as levenshtein_distance


def search(name_string, bin_to_id, id_to_name, gender=None, birthdate=None, similarity_threshold=60):
    # TODO should distinguish between first name (less reliable match) and other names.
    # consider storing in each bin, a namepart object linking to its name linking to its subject, that has name.isFirstName:bool

    # TODO consider searching per name alias instead of per candidate (list of aliases), requires a different data structure for lookups

    # 1. calculate the phonetics bins of the input name
    name_parts = [NamePart(name_string)]
    name_parts = normalizer.normalize_name_alias(NameAlias(name_parts, None))

    bins = set()
    for name_part in name_parts:
        name_part_bins = [b for b in dmeta(name_part) if b]  # dmeta sometimes outputs an empty 'None' bin, filter it out
        for bin in name_part_bins:
            bins.add((bin, name_part))

    # 2. find candidates with one or more matching bins
    candidates = set()
    name_parts_matched = set()
    bad_candidates = []  # candidates found to be bad matches for the query
    for (bin, name_part) in bins:
        if bin in bin_to_id:
            candidates_in_bin = bin_to_id[bin]
            for c in candidates_in_bin:
                (candidate_id, candidate_name_part) = c
                if candidate_id in bad_candidates:
                    # we already know this candidate is a bad match
                    continue

                (names, birthdates) = id_to_name[candidate_id]
                registered_genders = [g for g in [x.gender for x in names] if g]  # filter out None value for gender, i.e. unknown
                if gender and len(registered_genders) == 1 and gender not in registered_genders:
                    # mark the candidate as bad, so that we don't have to consider it again for this search query
                    bad_candidates.append(candidate_id)
                    continue  # skip to next candidate
                if birthdate and birthdates:
                    # exact birthdates are known
                    if birthdate not in birthdates:
                        # mark the candidate as bad, so that we don't have to consider it again for this search query
                        bad_candidates.append(candidate_id)
                        continue  # skip to next candidate
                # TODO also check birthdate ranges, or birthyear list only
                # TODO could optionally check birth country

                if levenshtein_distance.ratio(name_part, candidate_name_part) >= 0.6:  # do not add really bad matches
                    candidates.add(candidate_id)
                    name_parts_matched.add(name_part)

    # 3. calculate phonetic string similarity
    name_parts_missed = name_parts - name_parts_matched
    matching_character_count = sum(map(len, name_parts_matched))
    missing_character_count = sum(map(len, name_parts_missed))
    phonetic_similarity_ratio = 100 * matching_character_count / (matching_character_count + missing_character_count) if (matching_character_count + missing_character_count) else 0
    if phonetic_similarity_ratio < 25:  # performance: Early exit for really bad matches
        return []  # return no matches

    # 4. look up candidate names, filter out matches that are really bad, sort the remaining matches by similarity ratio
    normalized_query_name = " ".join(name_parts)
    # TODO word counts can be precomputed for better performance
    input_word_count = 1 if normalized_query_name.find(" ") < 0 else len(normalized_query_name.split())  # makes sure to split only on whitespace,
    short_name_length_limit = 12
    is_short_input_name = len(normalized_query_name) <= short_name_length_limit
    shortness = max(0, short_name_length_limit - len(normalized_query_name))

    filtered_candidates = []
    for candidate_id in candidates:
        list_subject = id_to_name[candidate_id]
        (list_subject_aliases, birthdays) = list_subject
        for candidate_name in list_subject_aliases:
            normalized_candidate_name = " ".join(normalizer.normalize_name_alias(candidate_name))  # TODO precompute this for better performance
            string_similarity = fuzz.token_sort_ratio(normalized_candidate_name, normalized_query_name)

            exact_match = string_similarity == 100
            similarity_score = string_similarity - 5

            if not exact_match:
                # 1. apply boosts:

                # boost phonetically similar matches
                boost_from_phonetic_similarity = similarity_threshold / 100.0 * phonetic_similarity_ratio / 16  # up to approx 6 points at 90% threshold
                similarity_score += boost_from_phonetic_similarity

                # 2. apply penalties:

                if is_short_input_name:
                    # TODO hackish, look for a better solution
                    # short matches must be extra good. Reduces false positives.
                    debuff = 2 * (similarity_threshold / 100.0) * shortness
                    similarity_score -= debuff

                # TODO word counts can be precomputed for better performance
                candidate_word_count = 1 if normalized_candidate_name.find(" ") < 0 else len(normalized_candidate_name.split())
                missing_words = abs(candidate_word_count - input_word_count)
                if missing_words:
                    missing_words_score = missing_words * 5 * similarity_threshold / 100.0
                    missing_words_penalty = min(20, missing_words_score)  # set a ceiling for the penalty
                    similarity_score -= missing_words_penalty  # 0 if missing 0 words, -4 if missing 2 words, etc

                # 3. normalize score after applying boosts and penalties
                similarity_score = max(0, min(similarity_score, 99.9))  # present all non-exact matches as no more than 99.9

            if similarity_score >= similarity_threshold:
                element = (candidate_id, similarity_score, candidate_name)
                filtered_candidates.append(element)

    filtered_candidates.sort(key=lambda tup: tup[1], reverse=True)  # sort by ratio, descending

    unique_candidates = []
    seen_candidates = set()
    for c in filtered_candidates:
        # only report one match against each list-subject, the best matching alias
        (candidate_id, similarity_score, candidate_name) = c
        if candidate_id not in seen_candidates:
            unique_candidates.append(c)
            seen_candidates.add(candidate_id)

    return unique_candidates


def print_longest_overflow_bin_length(bin_to_id, subjectType):
    longest_list = 0
    bin_of_longest_list = None
    for bin, references in bin_to_id.items():
        if len(references) > longest_list:
            longest_list = len(references)
            bin_of_longest_list = bin
    print("Longest overflow-bin for subject type {} had {} items. With value {}".format(subjectType, longest_list, bin_of_longest_list))


def memory_usage_resource():
    import resource  # not portable across platforms
    rusage_denom = 1024.
    if sys.platform == 'darwin':
        # ... it seems that in OSX the output is different units ...
        rusage_denom = rusage_denom * rusage_denom
    mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / rusage_denom
    return mem


import csv
import io
import sys


def import_test_subjects(filename):
    # reads a semi-colon value separated file, one person per list
    # format is firstname;lastname;birthdate;gender;customer_type
    with io.open(filename, 'r', newline='', encoding='utf-8') as csvfile:
        cvs_reader = csv.DictReader(csvfile, delimiter=';')
        try:
            subjects = []
            rows = list(cvs_reader)  # read it all into memory
            for row in rows:
                value = (row['firstname'], row['lastname'], row['birthdate'], row['gender'], row['id'], row['customer_type'])
                subjects.append(value)
            return subjects
        except csv.Error as e:
            sys.exit('file {}, line {}: {}'.format(filename, cvs_reader.line_num, e))


def execute_test_queries(id_to_name_persons):
    #filename = "test_queries.csv"
    filename = "internal_test_queries_sentry.csv"  # file intentionally not in git
    test_subjects = import_test_subjects(filename)
    test_subject_count = len(test_subjects)
    start = timer()
    total_matches = 0
    total_records = 0
    all_results = []
    counter = 0
    print("Searching for {} test-subjects read from file '{}'".format(test_subject_count, filename))
    for (firstname, lastname, birthdate, gender, id, customer_type) in test_subjects:
        workdone = counter / test_subject_count

        wholename = firstname + " " + lastname
        matches = search(wholename, bin_to_id_persons, id_to_name_persons, gender=gender, birthdate=birthdate, similarity_threshold=90)
        if matches:
            total_matches += 1
            total_records += len(matches)
            for m in matches:
                (candidate_id, similarity_score, candidate_name) = m
                result = (id, wholename, candidate_name, "OFAC_{}".format(candidate_id), customer_type, similarity_score)
                all_results.append(result)
        #print("\rProgress: [{0:50s}] {1:.1f}%".format('#' * int(workdone * 50), workdone * 100), end="", flush=True)
        counter += 1

    end = timer()
    time_use_s = end - start
    print("\n")  # end progress-line

    # sort the output on similarity ratio before printing
    #all_results.sort(key=lambda tup: tup[4], reverse=True)  # sort by ratio, descending
    all_results.sort(key=lambda tup: tup[1], reverse=False)  # sort by wholename, ascending
    for result in all_results:
        (id, wholename, candidate_name, list_entry_id, customer_type, similarity_score) = result
        print("{};{};{};{};{};{:.2f}".format(id, wholename, candidate_name, list_entry_id, customer_type, similarity_score))
    
    print("\nFound in total {} matches on {} list-subjects. Searched for {} customers.".format(total_records, total_matches, test_subject_count))
    print("Total time usage for searching: {}s ({}ns per query)".format(int(time_use_s + 0.5), int(10 ** 6 * time_use_s / test_subject_count + 0.5)))

def load_consolidated_sanctions(cons_filename='cons_advanced.xml'):
    consolidated_list = parser.parse(cons_filename, silence=True)
    return load_sanctions(consolidated_list)


def load_sdn_sanctions(sdn_filename='sdn_advanced_2024.xml'):
    sdn_list = parser.parse(sdn_filename, silence=True)
    return load_sanctions(sdn_list)

if __name__ == "__main__":
    #mem_start = memory_usage_resource()
    '''
    (id_to_name_persons_cons, id_to_name_entities_cons) = load_consolidated_sanctions()

    stop_words_persons = find_noise_words(id_to_name_persons_cons)
    stop_words_entities = find_noise_words(id_to_name_entities_cons)

    bin_to_id_persons = compute_phonetic_bin_lookup_table(id_to_name_persons_cons, stop_words_persons)
    bin_to_id_entities = compute_phonetic_bin_lookup_table(id_to_name_entities_cons, stop_words_entities)

    #print("Most common name parts for persons are", stop_words_persons)
    #print("Most common name parts for entities are", stop_words_entities)

    #print("Computed", len(bin_to_id_persons), "phonetic bins for", len(id_to_name_persons_cons), "list subjects of type person.")
    #print_longest_overflow_bin_length(bin_to_id_persons, "person")
    #print("Computed", len(bin_to_id_entities), "phonetic bins for", len(id_to_name_entities_cons), "list subjects of type entity.")
    #print_longest_overflow_bin_length(bin_to_id_entities, "entity")

    execute_test_queries(id_to_name_persons=id_to_name_persons_cons)

    '''
    (id_to_name_persons_sdn, id_to_name_entities_sdn) = load_sdn_sanctions(sdn_filename='sdn_advanced_2024.xml')

    stop_words_persons = find_noise_words(id_to_name_persons_sdn)
    stop_words_entities = find_noise_words(id_to_name_entities_sdn)

    bin_to_id_persons = compute_phonetic_bin_lookup_table(id_to_name_persons_sdn, stop_words_persons)
    bin_to_id_entities = compute_phonetic_bin_lookup_table(id_to_name_entities_sdn, stop_words_entities)

    print(len(id_to_name_persons_sdn))
    print(len(id_to_name_entities_sdn))

    #print("Most common name parts for persons are", stop_words_persons)
    #print("Most common name parts for entities are", stop_words_entities)

    #print("Computed", len(bin_to_id_persons), "phonetic bins for", len(id_to_name_persons_sdn), "list subjects of type person.")
    #print_longest_overflow_bin_length(bin_to_id_persons, "person")
    #print("Computed", len(bin_to_id_entities), "phonetic bins for", len(id_to_name_entities_sdn), "list subjects of type entity.")
    #print_longest_overflow_bin_length(bin_to_id_entities, "entity")

    execute_test_queries(id_to_name_persons=id_to_name_persons_sdn)
    
    #mem_end = memory_usage_resource()
    #print("Memory usage of sanction-list data structures are", mem_end - mem_start, "MB")

