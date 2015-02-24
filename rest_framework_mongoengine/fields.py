from django.core.exceptions import ValidationError
from django.utils.encoding import smart_str

from rest_framework import serializers
from bson.errors import InvalidId
from bson import DBRef, ObjectId

import numbers
import inspect
import json

from mongoengine import dereference
from mongoengine.base.document import BaseDocument
from mongoengine.document import Document, EmbeddedDocument
from mongoengine.fields import ObjectId

from collections import OrderedDict

from mongoengine import fields as me_fields
from rest_framework import fields as drf_fields
from rest_framework.fields import get_attribute, SkipField, empty
from rest_framework.utils import html

from rest_framework_mongoengine.utils import get_field_info

class DocumentField(serializers.Field):
    """
    Base field for Mongoengine fields that we can not convert to DRF fields.

    To Users:
        - You can subclass DocumentField to implement custom (de)serialization
    """

    type_label = 'DocumentField'

    def __init__(self, *args, **kwargs):

        self.depth = kwargs.pop('depth')
        try:
            self.model_field = kwargs.pop('model_field')
        except KeyError:
            raise ValueError("%s requires 'model_field' kwarg" % self.type_label)

        #better hotwire.
        self.dereference_refs = False

        super(DocumentField, self).__init__(*args, **kwargs)

    def get_subfields(self, model):
        model_fields = model._fields
        fields = {}
        for field_name in model_fields:
            fields[field_name] = self.get_subfield(model_fields[field_name])
        return fields

    def get_subfield(self, model_field):
        kwargs = self.get_subfield_kwargs(model_field)
        return get_field_mapping(model_field)(**kwargs)

    def get_subfield_kwargs(self, subfield):
        """
        Get kwargs that will be used for validation/serialization
        """
        kwargs = {}

        #kwargs to pass to all drfme fields
        #this includes lists, dicts, embedded documents, etc
        #depth included for flow control during recursive serialization.
        if is_drfme_field(subfield):
            kwargs['model_field'] = subfield
            kwargs['depth'] = self.depth - 1

        if type(subfield) is me_fields.ObjectIdField:
            kwargs['required'] = False
        else:
            kwargs['required'] = subfield.required

        if subfield.default:
            kwargs['required'] = False
            kwargs['default'] = subfield.default

        attribute_dict = {
            me_fields.StringField: ['max_length'],
            me_fields.DecimalField: ['min_value', 'max_value'],
            me_fields.EmailField: ['max_length'],
            me_fields.FileField: ['max_length'],
            me_fields.URLField: ['max_length'],
            me_fields.BinaryField: ['max_bytes']
        }

        #append any extra attributes based on the dict above, as needed.
        if subfield.__class__ in attribute_dict:
            attributes = attribute_dict[subfield.__class__]
            for attribute in attributes:
                if hasattr(subfield, attribute):
                    kwargs.update({attribute: getattr(subfield, attribute)})

        return kwargs

    def to_internal_value(self, data):
        return self.model_field.to_python(data)

    def to_representation(self, value):
        #transform_object(obj, depth)
        #We don't really ever want to hit this case, in theory.
        return smart_str(value) if isinstance(value, ObjectId) else value

class ReferenceField(DocumentField):
    """
    For ReferenceField.
    We always dereference DBRef object before serialization
    TODO: Maybe support DBRef too?
    """

    type_label = 'ReferenceField'

    def __init__(self, *args, **kwargs):
        super(ReferenceField, self).__init__(*args, **kwargs)

        self.model_cls = self.model_field.document_type

        #if depth is going to require we recurse, build a list of the child document's fields.
        if self.depth and self.dereference_refs:
            field_info = get_field_info(self.model_cls)
            self.child_fields = {}
            for field_name in field_info.fields_and_pk:
                model_field = field_info.fields_and_pk[field_name]

                #create the serializer field for this model_field
                field = self.get_subfield(model_field)

                self.child_fields[field_name] = field

    def to_internal_value(self, data):
        try:
            dbref = self.model_field.to_python(data)
        except InvalidId:
            raise ValidationError(self.error_messages['invalid_dbref'])

        return dbref


    def get_attribute(self, instance):
        #need to overwrite this, since drf's version
        #will call get_attr(instance, field_name), which dereferences ReferenceFields
        #even if we don't need them. We need it to be mindful of depth.
        if self.depth and self.dereference_refs:
            #TODO: fix to iterate properly?
            return super(DocumentField, self).get_attribute(instance)

        #return dbref by grabbing data directly, instead of going through the ReferenceField's __get__ method
        return instance._data[self.source]



    def to_representation(self, value):
        #value is either DBRef (if we're out of depth)
        #else a MongoEngine model reference.

        if value is None:
            return None

        if self.depth and self.dereference_refs:
            #get model's fields
            ret = OrderedDict()
            for field_name in value._fields:
                ret[field_name] = self.child_fields[field_name].to_representation(getattr(value, field_name))
            return ret
        elif isinstance(value, DBRef):
            return smart_str(value.id)
        else:
            return smart_str(value)



class ListField(DocumentField):

    type_label = 'ListField'

    def __init__(self, *args, **kwargs):
        super(ListField, self).__init__(*args, **kwargs)

        #instantiate the nested field
        nested_field_instance = self.model_field.field

        #initialize field
        self.nested_field = self.get_subfield(nested_field_instance)
        #and bind it, since that isn't being handled by the Serializer's BindingDict
        self.nested_field.bind('', self)

    def get_value(self, dictionary):
        # We override the default field access in order to support
        # lists in HTML forms.
        if html.is_html_input(dictionary):
            return html.parse_html_list(dictionary, prefix=self.field_name)
        value = dictionary.get(self.field_name, empty)
        #if isinstance(value, type('')):
        #    return json.loads(value)
        return value

    def to_internal_value(self, data):
        """
        List of dicts of native values <- List of dicts of primitive datatypes.
        """
        if html.is_html_input(data):
            data = html.parse_html_list(data)
        if isinstance(data, type('')) or not hasattr(data, '__iter__'):
            self.fail('not_a_list', input_type=type(data).__name__)
        return [self.nested_field.run_validation(item) for item in data]

    def get_attribute(self, instance):
        #since this is a passthrough, be careful about dereferencing the contents.
        if not self.dereference_refs and isinstance(self.nested_field, ReferenceField):
            #return data by grabbing it directly, instead of going through the field's __get__ method
            return instance._data[self.source]
        return super(DocumentField, self).get_attribute(instance)


    def to_representation(self, value):
        return [self.nested_field.to_representation(v) for v in value]


class MapField(ListField):
    type_label = "MapField"

    def to_internal_value(self, data):
        """
        List of dicts of native values <- List of dicts of primitive datatypes.
        """
        if html.is_html_input(data):
            data = html.parse_html_list(data)
        if isinstance(data, type('')) or not hasattr(data, '__iter__'):
            self.fail('not_a_dict', input_type=type(data).__name__)

        native = OrderedDict()
        for key in data:
            native[key] = self.nested_field.run_validation(data[key])
        return native

    def to_representation(self, value):

        ret = OrderedDict()
        for key in value:
            ret[key] = self.nested_field.to_representation(value[key])
        return ret

class EmbeddedDocumentField(DocumentField):

    type_label = 'EmbeddedDocumentField'

    def __init__(self, *args, **kwargs):
        self.ignore_depth = False #set this from a kwarg!

        super(EmbeddedDocumentField, self).__init__(*args, **kwargs)
        self.document_type = self.model_field.document_type

        #if depth is going to require we recurse, build a list of the embedded document's fields.
        if self.depth or self.ignore_depth:
            field_info = get_field_info(self.document_type)
            self.child_fields = {}
            for field_name in field_info.fields:
                model_field = field_info.fields[field_name]

                #create the serializer field for this model_field
                field = self.get_subfield(model_field)
                field.bind("field_name", self)

                self.child_fields[field_name] = field

    def get_attribute(self, instance):
        #return dict of whatever our fields pass back to us..
        ret = OrderedDict()

        if self.depth or self.ignore_depth:
            for field_name in self.child_fields:
                field = self.child_fields[field_name]
                ret[field_name] = field.get_attribute(instance[self.source])
            return ret

        else:
            return ret #"something else here?"


    def to_representation(self, value):
        if value is None:
            return None
        elif self.depth or self.ignore_depth:
            #get model's fields
            ret = OrderedDict()
            for field_name in self.child_fields:
                if value._data[field_name] is None:
                    ret[field_name] = None
                else:
                    ret[field_name] = self.child_fields[field_name].to_representation(value._data[field_name])
            return ret
        else:
            return "<<Embedded Document (Maximum recursion depth exceeded)>>"

    def to_internal_value(self, data):
        return self.model_field.to_python(data)


class DynamicField(DocumentField):

    type_label = 'DynamicField'

    def __init__(self, field_name=None, source=None, *args, **kwargs):
        super(DynamicField, self).__init__(*args, **kwargs)
        self.field_name = field_name
        self.source = source
        if source:
            self.source_attrs = self.source.split('.')

    def to_representation(self, value):
        #probably should do something a bit smarter?
        return self.model_field.to_python(value)



class DictField(DocumentField):

    type_label = "DictField"
    serializers = {}

    def __init__(self, *args, **kwargs):
        self.ignore_depth = False #set this from a kwarg!

        super(DictField, self).__init__(*args, **kwargs)

    def get_attribute(self, instance):

        #return dict as provided by the instance.
        return instance._data[self.source]

    def to_representation(self, value):
        ret = OrderedDict()

        for key in value:
            item = value[key]
            if isinstance(item, BaseDocument):
                if self.depth and not self.ignore_depth:
                    #serialize-on-the-fly! (patent pending)
                    cls = item.__class__
                    if type(cls) not in self.serializers:
                        self.serializers[cls] = self.get_subfields(cls)
                    fields = self.serializers[cls]

                    sub_ret = OrderedDict()
                    for field in fields:
                        field_value = item._data[field]
                        sub_ret[field] = fields[field].to_representation(field_value)

                    ret[key] = sub_ret
                else:
                    #out of depth.
                    ret[key] = "OUT OF DEPTH"
            elif isinstance(item, DBRef):
                ret[key] = smart_str(item.id)
            else:
                ret[key] = item
        if len(ret):
            raise undead
        return ret

    def to_internal_value(self, data):
        return self.model_field.to_python(data)


class ObjectIdField(DocumentField):

    type_label = 'ObjectIdField'

    def to_representation(self, value):
        return smart_str(value)

    def to_internal_value(self, data):
        return ObjectId(data)


class BinaryField(DocumentField):

    type_label = 'BinaryField'

    def __init__(self, **kwargs):
        try:
            self.max_bytes = kwargs.pop('max_bytes')
        except KeyError:
            raise ValueError('BinaryField requires "max_bytes" kwarg')
        super(BinaryField, self).__init__(**kwargs)

    def to_representation(self, value):
        return smart_str(value)

    def to_internal_value(self, data):
        return super(BinaryField, self).to_internal_value(smart_str(data))


class BaseGeoField(DocumentField):

    type_label = 'BaseGeoField'

def is_drfme_field(field):
    """
    :param field: Model field instance (or class)
    :return: True if field maps to a subclass of DocumentField, otherwise returns False.
    """

    #We will need the field class to look it up, so make sure we weren't passed one initially
    #and convert it if needed.
    if not isinstance(field, type):
        field = type(field)

    #if this is a key in DRFME_FIELD_MAPPING, return True
    if field in DRFME_FIELD_MAPPING:
        return True
    elif set(inspect.getmro(field)).intersection(DRFME_FIELD_MAPPING.keys()):
        #if the set of field's parent classes has an intersection with the keys in DRFME_FIELD_MAPPING
        #i.e. One of our parent classes is a type that needs handling with a DocumentField
        return True
    return False

def get_field_mapping(field):
    #given a field, look up the proper default drf or drf-me field

    #convert to class, if we're passed an instance, as above.
    if not isinstance(field, type):
        field = type(field)

    for cls in inspect.getmro(field):
        if cls in ME_FIELD_MAPPING:
            return ME_FIELD_MAPPING[cls]
    return None

DRFME_FIELD_MAPPING = {
    me_fields.ObjectIdField: ObjectIdField,
    me_fields.ReferenceField: ReferenceField,
    me_fields.ListField: ListField,
    me_fields.EmbeddedDocumentField: EmbeddedDocumentField,
    me_fields.DynamicField: DynamicField,
    me_fields.DictField: DictField,
    me_fields.MapField: MapField,
    me_fields.BinaryField: BinaryField,
    me_fields.GeoPointField: BaseGeoField,
    me_fields.PointField: BaseGeoField,
    me_fields.PolygonField: BaseGeoField,
    me_fields.LineStringField: BaseGeoField,
}

ME_FIELD_MAPPING = {
        me_fields.FloatField: drf_fields.FloatField,
        me_fields.IntField: drf_fields.IntegerField,
        me_fields.DateTimeField: drf_fields.DateTimeField,
        me_fields.EmailField: drf_fields.EmailField,
        me_fields.URLField: drf_fields.URLField,
        me_fields.StringField: drf_fields.CharField,
        me_fields.BooleanField: drf_fields.BooleanField,
        me_fields.FileField: drf_fields.FileField,
        me_fields.ImageField: drf_fields.ImageField,
        me_fields.UUIDField: drf_fields.CharField,
        me_fields.DecimalField: drf_fields.DecimalField
    }

ME_FIELD_MAPPING.update(DRFME_FIELD_MAPPING)