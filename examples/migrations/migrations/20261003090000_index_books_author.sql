-- no-transaction
CREATE INDEX CONCURRENTLY books_author_id ON books (author_id);
