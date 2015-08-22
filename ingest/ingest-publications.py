__author__ = 'szednik'

from SPARQLWrapper import SPARQLWrapper, JSON
import json
from rdflib import Namespace, RDF
import multiprocessing
import itertools
import argparse
import requests


def load_file(filepath):
    with open(filepath) as _file:
        return _file.read().replace('\n', " ")


get_publications_query = load_file("queries/listPublications.rq")
describe_publication_query = load_file("queries/describePublication.rq")

vivo_endpoint = "http://deepcarbon.tw.rpi.edu:3030/VIVO/query"
sparql = SPARQLWrapper(vivo_endpoint)

PROV = Namespace("http://www.w3.org/ns/prov#")
BIBO = Namespace("http://purl.org/ontology/bibo/")
VCARD = Namespace("http://www.w3.org/2006/vcard/ns#")
VIVO = Namespace('http://vivoweb.org/ontology/core#')
VITRO = Namespace("http://vitro.mannlib.cornell.edu/ns/vitro/0.7#")
OBO = Namespace("http://purl.obolibrary.org/obo/")
DCO = Namespace("http://info.deepcarbon.net/schema#")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")


def get_metadata(id):
    return {"index": {"_index": "dco", "_type": "publication", "_id": id}}


def get_id(dco_id):
    return dco_id[dco_id.rfind('/') + 1:]


def select(query):
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)
    results = sparql.query().convert()
    return results["results"]["bindings"]


def get_publications():
    r = select(get_publications_query)
    return [rs["publication"]["value"] for rs in r]


def process_publication(publication):
    pub = create_publication_doc(publication)
    if "dcoId" in pub and pub["dcoId"] is not None:
        return [json.dumps(get_metadata(get_id(pub["dcoId"]))), json.dumps(pub)]
    else:
        return []


def describe_publication(publication):
    q = describe_publication_query.replace("?publication", "<"+publication+">")
    sparql.setQuery(q)

    try:
        return sparql.query().convert()
    except RuntimeWarning:
        pass


def create_publication_doc(publication):
    graph = describe_publication(publication)

    pub = graph.resource(publication)

    try:
        title = pub.label().toPython()
    except AttributeError:
        print("missing title:", publication)
        return {}

    dco_id = list(pub.objects(DCO.hasDcoId))
    dco_id = str(dco_id[0].identifier) if dco_id else None

    is_dco_publication = list(pub.objects(DCO.isDCOPublication))
    is_dco_publication = True if is_dco_publication and is_dco_publication[0].toPython() == "YES" else False

    doc = {"uri": publication, "title": title, "dcoId": dco_id, "isDcoPublication": is_dco_publication}

    doi = list(pub.objects(BIBO.doi))
    doi = doi[0].toPython() if doi else None
    if doi:
        doc.update({"doi": doi})

    abstract = list(pub.objects(BIBO.abstract))
    abstract = abstract[0].toPython() if abstract else None
    if abstract:
        doc.update({"abstract": abstract})

    most_specific_type = list(pub.objects(VITRO.mostSpecificType))
    most_specific_type = most_specific_type[0].label().toPython() if most_specific_type and most_specific_type[0].label() else None
    if most_specific_type:
            doc.update({"mostSpecificType": most_specific_type})

    publication_year = list(pub.objects(DCO.yearOfPublication))
    publication_year = publication_year[0] if publication_year else None
    if publication_year:
        doc.update({"publicationYear": str(publication_year)})

    dco_community = list(pub.objects(DCO.associatedDCOCommunity))
    dco_community = dco_community[0] if dco_community else None
    if dco_community and dco_community.label():
        doc.update({"community": {"uri": str(dco_community.identifier), "name": dco_community.label().toPython()}})
    elif dco_community:
        print("community label missing:", str(dco_community.identifier))

    event = list(pub.objects(BIBO.presentedAt))
    event = event[0] if event else None
    if event and event.label():
        doc.update({"presentedAt": {"uri": str(event.identifier), "name": event.label().toPython()}})
    elif event:
        print("event missing label:", str(event.identifier))

    venue = list(pub.objects(VIVO.hasPublicationVenue))
    venue = venue[0] if venue else None
    if venue and venue.label():
        doc.update({"publishedIn": {"uri": str(venue.identifier), "name": venue.label().toPython()}})
    elif venue:
        print("venue missing label:", str(venue.identifier))

    subject_areas = []
    for subject_area in pub.objects(VIVO.hasSubjectArea):
        subject_areas.append({"uri": str(subject_area.identifier), "name": subject_area.label().toPython()})

    if subject_areas:
        doc.update({"subjectArea": subject_areas})

    authors = []
    authorships = [faux for faux in pub.objects(VIVO.relatedBy) if has_type(faux, VIVO.Authorship)]
    for authorship in authorships:

        author = [person for person in authorship.objects(VIVO.relates) if has_type(person, FOAF.Person)][0]
        name = author.label().toPython() if author else None

        obj = {"uri": str(author.identifier), "name": name}

        rank = list(authorship.objects(VIVO.rank))
        rank = rank[0].toPython() if rank else None
        if rank:
            obj.update({"rank": rank})

        research_areas = [research_area.label().toPython() for research_area in author.objects(VIVO.hasResearchArea)]
        if research_areas:
            obj.update({"researchArea": research_areas})

        authors.append(obj)

        org = list(author.objects(DCO.inOrganization))
        org = org[0] if org else None
        if org and org.label():
            obj.update({"organization": {"uri": str(org.identifier), "name": org.label().toPython()}})

    try:
        authors = sorted(authors, key=lambda a: a["rank"]) if len(authors) > 1 else authors
    except KeyError:
        print("missing rank for one or more authors of:", publication)

    doc.update({"authors": authors})

    return doc


def has_type(resource, type):
    for rtype in resource.objects(RDF.type):
        if str(rtype.identifier) == str(type):
            return True
    return False


def publish(bulk, endpoint, rebuild, mapping):

    # if configured to rebuild_index
    # Delete and then re-create to publication index (via PUT request)

    index_url = endpoint+"/dco"

    if rebuild:
        requests.delete(index_url)
        r = requests.put(index_url)
        if r.status_code != requests.codes.ok:
            print(r.url, r.status_code)
            r.raise_for_status()

    # push current publication document mapping

    mapping_url = endpoint+"/dco/publication/_mapping"
    with open(mapping) as mapping_file:
        r = requests.put(mapping_url, data=mapping_file)
        if r.status_code != requests.codes.ok:

            # new mapping may be incompatible with previous
            # delete current mapping and re-push

            requests.delete(mapping_url)
            r = requests.put(mapping_url, data=mapping_file)
            if r.status_code != requests.codes.ok:
                print(r.url, r.status_code)
                r.raise_for_status()

    # bulk import new publication documents
    bulk_import_url = endpoint+"/_bulk"
    r = requests.post(bulk_import_url, data=bulk)
    if r.status_code != requests.codes.ok:
        print(r.url, r.status_code)
        r.raise_for_status()


def generate(threads):
    pool = multiprocessing.Pool(threads)
    return list(itertools.chain.from_iterable(pool.map(process_publication, get_publications())))


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('--threads', default=8, help='number of threads to use (default = 8)')
    parser.add_argument('--es', default="http://data.deepcarbon.net/es", help="elasticsearch service URL")
    parser.add_argument('--publish', default=False, action="store_true", help="publish to elasticsearch?")
    parser.add_argument('--rebuild', default=False, action="store_true", help="rebuild elasticsearch index?")
    parser.add_argument('--mapping', default="../mappings/publication.json", help="publication elasticsearch mapping document")
    parser.add_argument('out', metavar='OUT', help='elasticsearch bulk ingest file')

    args = parser.parse_args()

    # generate bulk import document for publications
    records = generate(threads=args.threads)

    # save generated bulk import file so it can be backed up or reviewed if there are publish errors
    with open(args.out, "w") as bulk_file:
        bulk_file.write('\n'.join(records))

    # publish the results to elasticsearch if "--publish" was specified on the command line
    if args.publish:
        publish(bulk=records, endpoint=args.es, rebuild=args.rebuild, mapping=args.mapping)
