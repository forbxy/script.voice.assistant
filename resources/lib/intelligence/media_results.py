"""Internal search results; sources never decide when to play or change the UI."""


class SearchResult:
    def __init__(self, source='local', rows=(), automatic=False, exact=False,
                 complete=True, matching=None, movie_exact=False, client=None, state=None):
        self.source, self.rows = source, list(rows)
        self.automatic, self.exact, self.complete = automatic, exact, complete
        self.matching, self.movie_exact, self.client = matching, movie_exact, client
        self.state = state or ('matches' if self.rows else 'empty')
