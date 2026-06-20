"""GitHub GraphQL queries for history ingestion (Slice 5b, ADR 0011).

Three top-level queries — commits, pull requests, issues — each paginated via
GraphQL's cursor model. Each returns a `rateLimit` block so the orchestrator
can decide when to pause. Page sizes are conservative to keep node-cost low
(GraphQL has a per-query node-count budget separate from the 5000-point/hr
rate limit).
"""

# Commits on the default branch within a `since` window. `oid` is the SHA;
# parents are bounded at 10 (a normal merge has 2; 10 is paranoia).
COMMITS_QUERY = """\
query Commits($owner: String!, $name: String!, $since: GitTimestamp!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, since: $since, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              oid
              message
              authoredDate
              committedDate
              author { name email }
              additions
              deletions
              changedFilesIfAvailable
              parents(first: 10) { nodes { oid } }
            }
          }
        }
      }
    }
  }
  rateLimit { cost limit remaining resetAt }
}
"""

# Pull requests, newest-updated first. Pulls top-level comments + the
# top-level body of each review (inline review-thread comments are deferred
# — they're a per-thread per-line traversal and not load-bearing for v1).
# Filtered client-side by mergedAt/createdAt vs. window cutoff — GraphQL
# can't filter PRs directly on those fields. The orchestrator stops paging
# once it sees a page whose newest item is older than the cutoff.
PULL_REQUESTS_QUERY = """\
query PullRequests($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 25, orderBy: {field: UPDATED_AT, direction: DESC}, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        state
        author { login }
        createdAt
        updatedAt
        mergedAt
        closedAt
        mergeCommit { oid }
        baseRefName
        headRefName
        additions
        deletions
        changedFiles
        comments(first: 100) {
          nodes {
            databaseId
            body
            author { login }
            createdAt
          }
        }
        reviews(first: 50) {
          nodes {
            databaseId
            body
            author { login }
            createdAt
          }
        }
      }
    }
  }
  rateLimit { cost limit remaining resetAt }
}
"""

# Issues (non-PR), newest-updated first.
ISSUES_QUERY = """\
query Issues($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 25, orderBy: {field: UPDATED_AT, direction: DESC}, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        state
        author { login }
        createdAt
        updatedAt
        closedAt
        labels(first: 30) { nodes { name } }
        comments(first: 100) {
          nodes {
            databaseId
            body
            author { login }
            createdAt
          }
        }
      }
    }
  }
  rateLimit { cost limit remaining resetAt }
}
"""
