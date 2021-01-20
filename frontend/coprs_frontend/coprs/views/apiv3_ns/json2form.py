import flask
from werkzeug.datastructures import MultiDict


def get_form_compatible_data(preserve=None):
    input = without_empty_fields(get_input_dict())
    output = dict(input).copy()

    for k, v in input.items():
        # Preserve the original value and return it unchanged
        if k in (preserve or []):
            continue

        # Transform lists to strings separated with spaces
        if type(v) == list:
            v = " ".join(map(str, v))

        output[k] = v

    # Our WTForms expect chroots to be this way
    # We don't need this workaround for `BuildForm*` forms anymore but we still
    # need it for `CoprForm`
    for chroot in input.get("chroots") or []:
        output[chroot] = True

    output.update(flask.request.files or {})
    return MultiDict(output)


def get_input_dict():
    return flask.request.json or flask.request.form


def get_input():
    return MultiDict(get_input_dict())


def without_empty_fields(input):
    output = input.copy()
    for k, v in input.items():
        # Field with None value is like if it wasn't send with forms
        if v is None:
            del output[k]
    return output
