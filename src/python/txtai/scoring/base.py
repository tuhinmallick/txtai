"""
Scoring module
"""

import math
import os
import pickle

from collections import Counter
from multiprocessing.pool import ThreadPool

import numpy as np

from ..pipeline import Tokenizer
from ..version import __pickle__

from .terms import Terms


class Scoring:
    """
    Base scoring. Uses term frequency-inverse document frequency (TF-IDF).
    """

    def __init__(self, config=None):
        """
        Initializes backing statistic objects.

        Args:
            config: input configuration
        """

        # Scoring configuration
        self.config = config if config else {}

        # Document stats
        self.total = 0
        self.tokens = 0
        self.avgdl = 0

        # Word frequency
        self.docfreq = Counter()
        self.wordfreq = Counter()
        self.avgfreq = 0

        # IDF index
        self.idf = {}
        self.avgidf = 0

        # Tag boosting
        self.tags = Counter()

        # Tokenizer, lazily loaded as needed
        self.tokenizer = None

        # Transform columns
        columns = config.get("columns", {})
        self.text = columns.get("text", "text")
        self.object = columns.get("object", "object")

        # Term index
        self.terms = Terms(self.config["terms"], self.score, self.idf) if self.config.get("terms") else None

        # Document data
        self.documents = {} if self.config.get("content") else None

        # Normalize scores
        self.normalize = self.config.get("normalize")
        self.avgscore = None

    def insert(self, documents, index=None):
        """
        Inserts documents into the scoring index.

        Args:
            documents: list of (id, dict|text|tokens, tags)
            index: indexid offset
        """

        # Insert documents, calculate word frequency, total tokens and total documents
        for uid, document, tags in documents:
            # Extract text, if necessary
            if isinstance(document, dict):
                document = document.get(self.text, document.get(self.object))

            if document is not None:
                # If index is passed, use indexid, otherwise use id
                uid = index if index is not None else uid

                # Add entry to index if the data type is accepted
                if isinstance(document, (str, list)):
                    # Store content
                    if self.documents is not None:
                        self.documents[uid] = document

                    # Convert to tokens, if necessary
                    tokens = self.tokenize(document) if isinstance(document, str) else document

                    # Add tokens for id to term index
                    if self.terms is not None:
                        self.terms.insert(uid, tokens)

                    # Add tokens and tags to stats
                    self.addstats(tokens, tags)

                # Increment index
                index = index + 1 if index is not None else None

    def delete(self, ids):
        """
        Deletes documents from scoring index.

        Args:
            ids: list of ids to delete
        """

        # Delete from terms index
        if self.terms:
            self.terms.delete(ids)

        # Delete content
        if self.documents:
            for uid in ids:
                self.documents.pop(uid)

    def index(self, documents=None):
        """
        Indexes a collection of documents using a scoring method.

        Args:
            documents: list of (id, dict|text|tokens, tags)
        """

        # Insert documents
        if documents:
            self.insert(documents)

        # Build index if tokens parsed
        if self.wordfreq:
            # Calculate total token frequency
            self.tokens = sum(self.wordfreq.values())

            # Calculate average frequency per token
            self.avgfreq = self.tokens / len(self.wordfreq.values())

            # Calculate average document length in tokens
            self.avgdl = self.tokens / self.total

            # Compute IDF scores
            idfs = self.computeidf(np.array(list(self.docfreq.values())))
            for x, word in enumerate(self.docfreq):
                self.idf[word] = idfs[x]

            # Average IDF score per token
            self.avgidf = np.mean(idfs)

            # Calculate average score across index
            self.avgscore = self.score(self.avgfreq, self.avgidf, self.avgdl)

            # Filter for tags that appear in at least 1% of the documents
            self.tags = Counter({tag: number for tag, number in self.tags.items() if number >= self.total * 0.005})

        # Index terms, if available
        if self.terms:
            self.terms.index()

    def upsert(self, documents=None):
        """
        Convience method for API clarity. Calls index method.

        Args:
            documents: list of (id, dict|text|tokens, tags)
        """

        self.index(documents)

    def weights(self, tokens):
        """
        Builds a weights vector for each token in input tokens.

        Args:
            tokens: input tokens

        Returns:
            list of weights for each token
        """

        # Document length
        length = len(tokens)

        # Calculate token counts
        freq = self.computefreq(tokens)
        freq = np.array([freq[token] for token in tokens])

        # Get idf scores
        idf = np.array([self.idf[token] if token in self.idf else self.avgidf for token in tokens])

        # Calculate score for each token, use as weight
        weights = self.score(freq, idf, length).tolist()

        # Boost weights of tag tokens to match the largest weight in the list
        if self.tags:
            if tags := {
                token: self.tags[token] for token in tokens if token in self.tags
            }:
                maxWeight = max(weights)
                maxTag = max(tags.values())

                weights = [max(maxWeight * (tags[tokens[x]] / maxTag), weight) if tokens[x] in tags else weight for x, weight in enumerate(weights)]

        return weights

    def search(self, query, limit=3):
        """
        Search index for documents matching query.

        Args:
            query: input query
            limit: maximum results

        Returns:
            list of (id, score) or (data, score) if content is enabled
        """

        # Check if term index available
        if self.terms:
            # Parse query into terms
            query = self.tokenize(query) if isinstance(query, str) else query

            # Get topn term query matches
            scores = self.terms.search(query, limit)

            # Normalize scores, if enabled
            if self.normalize and scores:
                # Calculate max score = best score for this query + average index score
                # Limit max to 6 * average index score
                maxscore = min(scores[0][1] + self.avgscore, 6 * self.avgscore)

                # Normalize scores between 0 - 1 using maxscore
                scores = [(x, min(score / maxscore, 1.0)) for x, score in scores]

            # Add content, if available
            return self.results(scores)

        return None

    def batchsearch(self, queries, limit=3, threads=True):
        """
        Search index for documents matching queries. This method is able to run as multiple threads due to
        a number of regex and numpy method calls that drop the GIL.
        """

        # Calculate number of threads using a thread per 25k records in index
        threads = math.ceil(self.count() / 25000) if isinstance(threads, bool) and threads else int(threads)
        threads = min(max(threads, 1), os.cpu_count())

        # Run threaded queries
        results = []
        with ThreadPool(threads) as pool:
            results.extend(iter(pool.starmap(self.search, [(x, limit) for x in queries])))
        return results

    def count(self):
        """
        Returns the total number of documents indexed.

        Returns:
            total number of documents indexed
        """

        return self.terms.count() if self.terms else self.total

    def load(self, path):
        """
        Loads a saved Scoring object from path.

        Args:
            path: directory path to load scoring index
        """

        with open(path, "rb") as handle:
            # Load scoring
            self.__dict__.update(pickle.load(handle))

            # Load terms
            if self.config.get("terms"):
                self.terms = Terms(self.config["terms"], self.score, self.idf)
                self.terms.load(f"{path}.terms")

    def save(self, path):
        """
        Saves a Scoring object to path.

        Args:
            path: directory path to save scoring index
        """

        with open(path, "wb") as handle:
            # Don't serialize following fields
            skipfields = ("config", "terms", "tokenizer")

            # Save scoring
            state = {key: value for key, value in self.__dict__.items() if key not in skipfields}
            pickle.dump(state, handle, protocol=__pickle__)

            # Save terms
            if self.terms:
                self.terms.save(f"{path}.terms")

    def close(self):
        """
        Close and free resources used by this instance.
        """

        if self.terms:
            self.terms.close()

    def hasterms(self):
        """
        Check if this scoring instance has an associated terms index.

        Returns:
            True if this scoring instance has an associated terms index.
        """

        return self.terms is not None

    def isnormalized(self):
        """
        Check if this scoring instance returns normalized scores.

        Returns:
            True if normalize is enabled, False otherwise
        """

        return self.normalize

    def computefreq(self, tokens):
        """
        Computes token frequency. Used for token weighting.

        Args:
            tokens: input tokens

        Returns:
            {token: count}
        """

        return Counter(tokens)

    def computeidf(self, freq):
        """
        Computes an idf score for word frequency.

        Args:
            freq: word frequency

        Returns:
            idf score
        """

        return np.log((self.total + 1) / (freq + 1)) + 1

    # pylint: disable=W0613
    def score(self, freq, idf, length):
        """
        Calculates a score for each token.

        Args:
            freq: token frequency
            idf: token idf score
            length: total number of tokens in source document

        Returns:
            token score
        """

        return idf * np.sqrt(freq) * (1 / np.sqrt(length))

    def addstats(self, tokens, tags):
        """
        Add tokens and tags to stats.

        Args:
            tokens: list of tokens
            tags: list of tags
        """

        # Total number of times token appears, count all tokens
        self.wordfreq.update(tokens)

        # Total number of documents a token is in, count unique tokens
        self.docfreq.update(set(tokens))

        # Get list of unique tags
        if tags:
            self.tags.update(tags.split())

        # Total document count
        self.total += 1

    def tokenize(self, text):
        """
        Tokenizes text using default tokenizer.

        Args:
            text: input text

        Returns:
            tokens
        """

        # Load tokenizer
        if not self.tokenizer:
            self.tokenizer = self.loadtokenizer()

        return self.tokenizer(text)

    def loadtokenizer(self):
        """
        Load default tokenizer.

        Returns:
            tokenize method
        """

        # Custom tokenizer settings
        if self.config.get("tokenizer"):
            return Tokenizer(**self.config.get("tokenizer"))

        # Terms index use a standard tokenizer
        return Tokenizer() if self.config.get("terms") else Tokenizer.tokenize

    def results(self, scores):
        """
        Resolves a list of (id, score) with document content, if available. Otherwise, the original input is returned.

        Args:
            scores: list of (id, score)

        Returns:
            resolved results
        """

        if self.documents:
            return [{"id": x, "text": self.documents[x], "score": score} for x, score in scores]

        return scores
