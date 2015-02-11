from django.core.exceptions import ValidationError
from django.utils.encoding import smart_str

from rest_framework import serializers
from bson.errors import InvalidId
from bson import DBRef, ObjectId

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
        self.field_mapping = ME_FIELD_MAPPING

        self.depth = kwargs.pop('depth')
        #hotwire
        self.depth = 0
        try:
            self.model_field = kwargs.pop('model_field')
        except KeyError:
            raise ValueError("%s requires 'model_field' kwarg" % self.type_label)

        super(DocumentField, self).__init__(*args, **kwargs)

    def remove_drfme_kwargs(self, kwargs):
        #clean out DRFME kwargs if we're calling a DRF field
        kwargs.pop('depth', None)
        kwargs.pop('model_field', None)
        kwargs.pop('document_type', None)
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
        if self.depth:
            field_info = get_field_info(self.model_cls)
            self.child_fields = {}
            for field_name in field_info.fields_and_pk:
                model_field = field_info.fields_and_pk[field_name]
                kwargs.update({
                    'depth': self.depth - 1,
                    'model_field': model_field
                })

                if model_field.__class__ not in DRFME_FIELD_MAPPING:
                    kwargs = self.remove_drfme_kwargs(kwargs)
                #create the serializer field for this model_field
                field = self.field_mapping[model_field.__class__](**kwargs)

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
        if not self.depth:
            #return dbref by grabbing data directly, instead of going through the ReferenceField's __get__ method
            return instance._data[self.source]

        return super(DocumentField, self).get_attribute(instance)


    def to_representation(self, value):
        #value is either DBRef (if we're out of depth)
        #else a MongoEngine model reference.

        if value is None:
            return None

        if isinstance(value, DBRef):
            return smart_str(value.id)
        else:
            #get model's fields
            ret = OrderedDict()
            for field_name in value._fields:
                ret[field_name] = self.child_fields[field_name].to_representation(getattr(value, field_name))
            return ret


class ListField(DocumentField):

    type_label = 'ListField'

    def __init__(self, *args, **kwargs):
        super(ListField, self).__init__(*args, **kwargs)

        #instantiate the nested field
        nested_field_instance = self.model_field.field
        nested_field_cls = nested_field_instance.__class__

        kwargs.update({
            'model_field': nested_field_instance
        })
        if self.field_mapping[nested_field_cls] in (EmbeddedDocumentField, ):
            #if the nested field is an embedded document, pass along its document_type
            kwargs['document_type'] = nested_field_instance.document_type
        elif not issubclass(self.field_mapping[nested_field_cls], DocumentField):
            #if nested class isn't a DocumentField, remove all the kwargs that may break it.
            kwargs = self.remove_drfme_kwargs(kwargs)

        #initialize field
        self.nested_field = self.field_mapping[nested_field_cls](**kwargs)
        #and bind it, since that isn't being handled by the Serializer's BindingDict
        self.nested_field.bind('', self)

    def get_value(self, dictionary):
        # We override the default field access in order to support
        # lists in HTML forms.
        if html.is_html_input(dictionary):
            value = html.parse_html_list(dictionary, prefix=self.field_name)
            return value
        return dictionary.get(self.field_name, empty)


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
        if not self.depth:
            #return data by grabbing it directly, instead of going through the field's __get__ method
            return instance._data[self.source]
        return super(DocumentField, self).get_attribute(instance)


    def to_representation(self, value):
        return [self.nested_field.to_representation(v) for v in value]


class EmbeddedDocumentField(DocumentField):

    type_label = 'EmbeddedDocumentField'

    def __init__(self, *args, **kwargs):

        try:
            self.document_type = kwargs.pop('document_type')
        except KeyError:
            raise ValueError("EmbeddedDocumentField requires 'document_type' kwarg")

        super(EmbeddedDocumentField, self).__init__(*args, **kwargs)

        #if depth is going to require we recurse, build a list of the embedded document's fields.
        #if self.depth:
        if True:
            field_info = get_field_info(self.document_type)
            self.child_fields = {}
            for field_name in field_info.fields:
                model_field = field_info.fields[field_name]
                kwargs.update({
                    'depth': self.depth,# - 1,
                    'model_field': model_field
                })

                if model_field.__class__ not in DRFME_FIELD_MAPPING:
                    kwargs = self.remove_drfme_kwargs(kwargs)
                #create the serializer field for this model_field
                field = self.field_mapping[model_field.__class__](**kwargs)
                field.bind("field_name", self)

                self.child_fields[field_name] = field

    def get_attribute(self, instance):
        #return dict of whatever our fields pass back to us..
        ret = OrderedDict()

        #if self.depth:
        if True:
            for field_name in self.child_fields:
                field = self.child_fields[field_name]
                ret[field_name] = field.get_attribute(instance[self.source])
            return ret

        else:
            return ret #"or something else here?"


    def to_representation(self, value):
        if value is None:
            return None
        #elif self.depth:
        else:
            #get model's fields
            ret = OrderedDict()
            for field_name in self.child_fields:
                ret[field_name] = self.child_fields[field_name].to_representation(value._data[field_name])
            return ret
        #else:
        #    return "<<Embedded Document (Maximum recursion depth exceeded)>>"

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
        return self.model_field.to_python(value)


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

DRFME_FIELD_MAPPING = {
    me_fields.ObjectIdField: ObjectIdField,
    me_fields.ReferenceField: ReferenceField,
    me_fields.ListField: ListField,
    me_fields.EmbeddedDocumentField: EmbeddedDocumentField,
    me_fields.DynamicField: DynamicField,
    me_fields.DictField: DocumentField,
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