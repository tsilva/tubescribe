// Only fixed, parameterized reader queries can be sealed. No client SQL is accepted.
const owner = `JOIN reader_library l
  ON l.id='archive' AND l.clerk_user_id=$1 AND l.issuer=$2`;

export const queries = Object.freeze({
  revision: `SELECT r.revision FROM catalogue_revisions r ${owner} WHERE r.scope='feed'`,
});
