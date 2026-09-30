ALTER TABLE trace_search_documents
    ADD COLUMN fuzzy_terms TEXT NOT NULL DEFAULT '';

UPDATE trace_search_documents
SET fuzzy_terms = array_to_string(
    tsvector_to_array(to_tsvector('simple'::regconfig, text)),
    ' '
);

CREATE FUNCTION trace_search_set_fuzzy_terms() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.fuzzy_terms := array_to_string(
        tsvector_to_array(to_tsvector('simple'::regconfig, NEW.text)),
        ' '
    );
    RETURN NEW;
END
$$;

CREATE TRIGGER trace_search_documents_fuzzy_terms
BEFORE INSERT OR UPDATE OF text ON trace_search_documents
FOR EACH ROW EXECUTE FUNCTION trace_search_set_fuzzy_terms();

CREATE INDEX trace_search_documents_fuzzy_terms_trigram
    ON trace_search_documents USING GIN(fuzzy_terms gin_trgm_ops);
