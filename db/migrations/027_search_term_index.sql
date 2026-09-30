ALTER TABLE trace_search_documents
    ADD COLUMN fuzzy_term_list TEXT[] NOT NULL DEFAULT '{}';

UPDATE trace_search_documents
SET fuzzy_term_list = CASE
    WHEN fuzzy_terms = '' THEN '{}'
    ELSE string_to_array(fuzzy_terms, ' ')
END;

CREATE INDEX trace_search_documents_fuzzy_term_list
    ON trace_search_documents USING GIN(fuzzy_term_list);

DROP TRIGGER trace_search_documents_fuzzy_terms ON trace_search_documents;
DROP FUNCTION trace_search_set_fuzzy_terms();
DROP INDEX trace_search_documents_fuzzy_terms_trigram;

CREATE FUNCTION trace_search_set_fuzzy_term_list() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.fuzzy_term_list := tsvector_to_array(
        to_tsvector('simple'::regconfig, NEW.text)
    );
    RETURN NEW;
END
$$;

CREATE TRIGGER trace_search_documents_fuzzy_term_list
BEFORE INSERT OR UPDATE OF text ON trace_search_documents
FOR EACH ROW EXECUTE FUNCTION trace_search_set_fuzzy_term_list();
