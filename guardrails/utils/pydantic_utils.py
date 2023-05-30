"""Utilities for working with Pydantic models.

Guardrails lets users specify

<pydantic
    model="Person"
    name="person"
    description="Information about a person."
    on-fail-pydantic="reask" / "refrain" / "raise"
/>
"""
import logging
from collections import defaultdict
from datetime import date, time
from typing import Any, Dict, Optional, Type, Union, get_args, get_origin

from griffe.dataclasses import Docstring
from griffe.docstrings.parsers import Parser, parse
from lxml.builder import E
from lxml.etree import Element
from pydantic import BaseModel, HttpUrl
from pydantic.fields import ModelField

griffe_docstrings_google_logger = logging.getLogger("griffe.docstrings.google")
griffe_agents_nodes_logger = logging.getLogger("griffe.agents.nodes")


def get_field_descriptions(model: "BaseModel") -> Dict[str, str]:
    """Get the descriptions of the fields in a Pydantic model using the
    docstring."""
    griffe_docstrings_google_logger.disabled = True
    griffe_agents_nodes_logger.disabled = True
    try:
        docstring = Docstring(model.__doc__, lineno=1)
    except AttributeError:
        return {}
    parsed = parse(docstring, Parser.google)
    griffe_docstrings_google_logger.disabled = False
    griffe_agents_nodes_logger.disabled = False

    # TODO: change parsed[1] to an isinstance check for the args section
    return {
        field.name: field.description.replace("\n", " ")
        for field in parsed[1].as_dict()["value"]
    }


PYDANTIC_SCHEMA_TYPE_MAP = {
    "string": "string",
    "number": "float",
    "integer": "integer",
    "boolean": "bool",
    "object": "object",
    "array": "list",
}

pydantic_validators = {}
pydantic_models = {}


# Create a class decorator to register all the validators in a BaseModel
def register_pydantic(cls: type):
    """
    Register a Pydantic BaseModel. This is a class decorator that can
    be used in the following way:

    ```
    @register_pydantic
    class MyModel(BaseModel):
        ...
    ```

    This decorator does the following:
        1. Add the model to the pydantic_models dictionary.
        2. Register all pre and post validators.
        3. Register all pre and post root validators.
    """
    # Register the model
    pydantic_models[cls.__name__] = cls

    # Create a dictionary to store all the validators
    pydantic_validators[cls] = {}
    # All all pre and post validators, for each field in the model
    for field in cls.__fields__.values():
        pydantic_validators[cls][field.name] = {}
        if field.pre_validators:
            for validator in field.pre_validators:
                pydantic_validators[cls][field.name][
                    validator.func_name.replace("_", "-")
                ] = validator
        if field.post_validators:
            for validator in field.post_validators:
                pydantic_validators[cls][field.name][
                    validator.func_name.replace("_", "-")
                ] = validator

    pydantic_validators[cls]["__root__"] = {}
    # Add all pre and post root validators
    if cls.__pre_root_validators__:
        for _, validator in cls.__pre_root_validators__:
            pydantic_validators[cls]["__root__"][
                validator.__name__.replace("_", "-")
            ] = validator

    if cls.__post_root_validators__:
        for _, validator in cls.__post_root_validators__:
            pydantic_validators[cls]["__root__"][
                validator.__name__.replace("_", "-")
            ] = validator
    return cls


def is_pydantic_base_model(type_annotation: Any) -> bool:
    """Check if a type_annotation is a Pydantic BaseModel."""
    try:
        if issubclass(type_annotation, BaseModel):
            return True
    except TypeError:
        False
    return False


def is_list(type_annotation: Any) -> bool:
    """Check if a type_annotation is a list."""

    type_annotation = prepare_type_annotation(type_annotation)

    if is_pydantic_base_model(type_annotation):
        return False
    if get_origin(type_annotation) == list:
        return True
    elif type_annotation == list:
        return True
    return False


def is_dict(type_annotation: Any) -> bool:
    """Check if a type_annotation is a dict."""

    type_annotation = prepare_type_annotation(type_annotation)

    if is_pydantic_base_model(type_annotation):
        return True
    if get_origin(type_annotation) == dict:
        return True
    elif type_annotation == dict:
        return True
    return False


def prepare_type_annotation(type_annotation: Any) -> Type:
    """Get the raw type annotation that can be used for downstream processing.

    This function does the following:
        1. If the type_annotation is a Pydantic field, get the annotation
        2. If the type_annotation is a Union, get the first non-None type

    Args:
        type_annotation (Any): The type annotation to prepare

    Returns:
        Type: The prepared type annotation
    """

    if isinstance(type_annotation, ModelField):
        type_annotation = type_annotation.annotation

    # Strip a Union type annotation to the first non-None type
    if get_origin(type_annotation) == Union:
        type_annotation = [t for t in get_args(type_annotation) if t != type(None)]
        assert (
            len(type_annotation) == 1
        ), "Union type must have exactly one non-None type"
        type_annotation = type_annotation[0]

    return type_annotation


def type_annotation_to_string(type_annotation: Any) -> str:
    """Map a type_annotation to the name of the corresponding field type.

    This function checks if the type_annotation is a list, dict, or a primitive type,
    and returns the corresponding type name, e.g. "list", "object", "bool", "date", etc.
    """

    # Get the type annotation from the type_annotation
    type_annotation = prepare_type_annotation(type_annotation)

    # Map the type annotation to the corresponding field type
    if is_list(type_annotation):
        return "list"
    elif is_dict(type_annotation):
        return "object"
    elif type_annotation == bool:
        return "bool"
    elif type_annotation == date:
        return "date"
    elif type_annotation == float:
        return "float"
    elif type_annotation == int:
        return "integer"
    elif type_annotation == str:
        return "string"
    elif type_annotation == time:
        return "time"
    elif type_annotation == HttpUrl:
        return "url"
    else:
        raise ValueError(f"Unsupported type: {type_annotation}")


def add_validators_to_xml_element(field_info: ModelField, element: Element) -> Element:
    """Extract validators from a pydantic ModelField and add to XML element

    Args:
        field_info: The field info to extract validators from
        element: The XML element to add the validators to

    Returns:
        The XML element with the validators added
    """

    if (
        isinstance(field_info, ModelField)
        and hasattr(field_info.field_info, "gd_validators")
        and field_info.field_info.gd_validators is not None
    ):
        format_prompt = []
        on_fails = {}
        for validator in field_info.field_info.gd_validators:
            validator_prompt = validator
            if not isinstance(validator, str):
                # `validator`` is of type gd.Validator, use the to_xml_attrib method
                validator_prompt = validator.to_xml_attrib()
                # Set the on-fail attribute based on the on_fail value
                on_fail = validator.on_fail.__name__ if validator.on_fail else "noop"
                on_fails[validator.rail_alias] = on_fail
            format_prompt.append(validator_prompt)

        if len(format_prompt) > 0:
            format_prompt = "; ".join(format_prompt)
            element.set("format", format_prompt)
            for rail_alias, on_fail in on_fails.items():
                element.set("on-fail-" + rail_alias, on_fail)

    return element


def create_xml_element_for_field(
    field: Union[ModelField, Type, type],
    field_name: Optional[str] = None,
) -> Element:
    """Create an XML element corresponding to a field.

    Args:
        field_info: Field's type. This could be a Pydantic ModelField or a type.
        field_name: Field's name. For some fields (e.g. list), this is not required.

    Returns:
        The XML element corresponding to the field.
    """

    # Create the element based on the field type
    field_type = type_annotation_to_string(field)
    element = E(field_type)

    # Add name attribute
    if field_name:
        element.set("name", field_name)

    # Add validators
    element = add_validators_to_xml_element(field, element)

    # Add description attribute
    if isinstance(field, ModelField):
        if field.field_info.description is not None:
            element.set("description", field.field_info.description)

    # Create XML elements for the field's children
    if field_type in ["list", "object"]:
        type_annotation = prepare_type_annotation(field)

        if is_list(type_annotation):
            inner_type = get_args(type_annotation)
            if len(inner_type) == 0:
                # If the list is empty, we cannot infer the type of the elements
                pass

            inner_type = inner_type[0]
            if is_pydantic_base_model(inner_type):
                object_element = create_xml_element_for_base_model(inner_type)
                element.append(object_element)
            else:
                inner_element = create_xml_element_for_field(inner_type)
                element.append(inner_element)

        elif is_dict(type_annotation):
            if is_pydantic_base_model(type_annotation):
                element = create_xml_element_for_base_model(type_annotation, element)
            else:
                dict_args = get_args(type_annotation)
                if len(dict_args) == 2:
                    key_type, val_type = dict_args
                    assert key_type == str, "Only string keys are supported for dicts"
                    inner_element = create_xml_element_for_field(val_type)
                    element.append(inner_element)
        else:
            raise ValueError(f"Unsupported type: {type_annotation}")

    return element


def create_xml_element_for_base_model(
    model: BaseModel, element: Optional[Element] = None
) -> Element:
    """Create an XML element for a Pydantic BaseModel.

    This function does the following:
        1. Iterates through fields of the model and creates XML elements for each field
        2. If a field is a Pydantic BaseModel, it creates a nested XML element
        3. If the BaseModel contains a field with a `when` attribute, it creates
           `Choice` and `Case` elements for the field.

    Args:
        model: The Pydantic BaseModel to create an XML element for
        element: The XML element to add the fields to. If None, a new XML element

    Returns:
        The XML element with the fields added
    """

    if element is None:
        element = E("object")

    # Identify fields with `when` attribute
    choice_elements = defaultdict(list)
    case_elements = set()
    for field_name, field in model.__fields__.items():
        if hasattr(field.field_info, "when") and field.field_info.when:
            choice_elements[field.field_info.when].append((field_name, field))
            case_elements.add(field_name)

    # Add fields to the XML element, except for fields with `when` attribute
    for field_name, field in model.__fields__.items():
        if field_name in choice_elements or field_name in case_elements:
            continue
        field_element = create_xml_element_for_field(field, field_name)
        element.append(field_element)

    # Add `Choice` and `Case` elements for fields with `when` attribute
    for when, discriminator_fields in choice_elements.items():
        choice_element = E("choice", name=when)
        # TODO(shreya): DONT MERGE WTHOUT SOLVING THIS: How do you set this via SDK?
        choice_element.set("on-fail-choice", "exception")

        for field_name, field in discriminator_fields:
            case_element = E("case", name=field_name)
            field_element = create_xml_element_for_field(field, field_name)
            case_element.append(field_element)
            choice_element.append(case_element)

        element.append(choice_element)

    return element
