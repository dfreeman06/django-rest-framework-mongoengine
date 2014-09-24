from bson.errors import InvalidId
from django.core.exceptions import ValidationError
from django.utils.encoding import smart_str
from mongoengine import dereference
from mongoengine.base import get_document
from mongoengine.errors import DoesNotExist
from mongoengine.base.document import BaseDocument
from mongoengine.document import Document
from mongoengine import fields
from rest_framework import serializers
from mongoengine.fields import ObjectId

import sys
from bson import DBRef
from rest_framework.reverse import reverse
from django.core.urlresolvers import resolve, get_script_prefix, NoReverseMatch

if sys.version_info[0] >= 3:
    def unicode(val):
        return str(val)


class MongoDocumentField(serializers.WritableField):
    MAX_RECURSION_DEPTH = 5  # default value of depth
    HYPERLINK = False

    def __init__(self, *args, **kwargs):
        try:
            self.model_field = kwargs.pop('model_field')
            self.depth = kwargs.pop('depth', self.MAX_RECURSION_DEPTH)
        except KeyError:
            raise ValueError("%s requires 'model_field' kwarg" % self.type_label)

        super(MongoDocumentField, self).__init__(*args, **kwargs)

    def transform_document(self, document, depth):
        data = {}

        # serialize each required field
        for name, field in document._fields.iteritems():
            if hasattr(document, smart_str(name)):
                # finally check for an attribute 'field' on the instance
                obj = getattr(document, name)
                if obj and isinstance(field, fields.ReferenceField) and not self.HYPERLINK:
                    obj = self.uri_or_obj(obj, depth-1, field.document_type)
            else:
                continue

            val = self.transform_object(obj, depth-1)

            if val is not None:
                data[name] = val

        return data

    def transform_dict(self, obj, depth):
        return dict([(key, self.transform_object(val, depth-1))
                     for key, val in obj.items()])

    def transform_object(self, obj, depth):
        """
        Models to natives
        Recursion for (embedded) objects
        """
        if depth == 0:
            # Return primary key if exists, else return default text
            return str(getattr(obj, 'pk', "Max recursion depth exceeded"))
        elif isinstance(obj, DBRef):
            return self.uri_or_obj(obj, depth-1)
        elif isinstance(obj, BaseDocument):
            # Document, EmbeddedDocument
            return self.transform_document(obj, depth-1)
        elif isinstance(obj, dict):
            # Dictionaries
            return self.transform_dict(obj, depth-1)
        elif isinstance(obj, list):
            # List
            return [self.transform_object(value, depth-1) for value in obj]
        elif obj is None:
            return None
        else:
            return unicode(obj) if isinstance(obj, ObjectId) else obj

    def uri_or_obj(self, obj, depth, doc_type=None):
        try:
            document_type = doc_type or self.document_type()
            lookup_field = self.context['view'].lookup_field
            kwargs = {lookup_field: str(obj.id)}

            # view_name = self.view_name
            request = self.context.get('request', None)
            format = self.context.get('format', None)

            view_name = self.parent._get_default_view_name(document_type)
            return reverse(view_name, kwargs=kwargs, request=request, format=format)
        except NoReverseMatch:
            try:
                return self.transform_object(document_type.objects.get(id=obj.id), depth)
            except DoesNotExist:
                pass

    def document_type(self):
        return self.model_field.document_type



class ReferenceField(MongoDocumentField):

    type_label = 'ReferenceField'

    def from_native(self, value):
        try:
            dbref = self.model_field.to_python(value)
        except InvalidId:
            raise ValidationError(self.error_messages['invalid'])
        instance = self.model_field.document_type.objects.get(id=dbref.id)
        # Check if dereference was successful
        if not isinstance(instance, Document):
            msg = self.error_messages['invalid']
            raise ValidationError(msg)
        return instance

    def to_native(self, obj):
        return self.transform_object(obj, self.depth)


class ListField(MongoDocumentField):

    type_label = 'ListField'

    def from_native(self, value):
        return self.model_field.to_python(value)

    def to_native(self, obj):
        return self.transform_object(obj, self.depth)

    def document_type(self):
        return self.model_field.field.document_type


class EmbeddedDocumentField(MongoDocumentField):

    type_label = 'EmbeddedDocumentField'

    def __init__(self, *args, **kwargs):
        try:
            self.document_type = kwargs.pop('document_type')
        except KeyError:
            raise ValueError("EmbeddedDocumentField requires 'document_type' kwarg")

        super(EmbeddedDocumentField, self).__init__(*args, **kwargs)

    def get_default_value(self):
        return self.to_native(self.default())

    def to_native(self, obj):
        if obj is None:
            return None
        else:
            return self.transform_object(obj, self.depth)

    def from_native(self, value):
        return self.model_field.to_python(value)


class DynamicField(MongoDocumentField):

    type_label = 'DynamicField'

    def to_native(self, obj):
        return self.transform_object(obj, self.depth)


class MapField(DynamicField):

    type_label = 'MapField'

    def document_type(self):
        return self.model_field.field.document_type