CREATE TABLE note_tags (
    note_id integer NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
    tag     text NOT NULL,
    PRIMARY KEY (note_id, tag)
);
