# -*- coding: utf-8; -*-
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

import superdesk
import logging
from copy import deepcopy
from superdesk import get_resource_service
from superdesk.errors import SuperdeskApiError
from superdesk.media.media_operations import crop_image, process_file_from_stream
from superdesk.upload import url_for_media
from superdesk.metadata.item import CONTENT_TYPE, ITEM_TYPE
from.renditions import _resize_image


logger = logging.getLogger(__name__)


class CropService():

    crop_sizes = []

    def validate_crop(self, original, updates, crop_name):
        """
        :param dict original: original item
        :param dict updates: updated renditions
        :param str crop_name: name of the crop
        :param dict doc: crop co-ordinates
        :raises SuperdeskApiError.badRequestError:
            For following conditions:
            1) if type != picture
            2) if renditions are missing in the original image
            3) if original rendition is missing
            4) Crop name is invalid
        """
        # Check if type is picture
        if original[ITEM_TYPE] != CONTENT_TYPE.PICTURE:
            raise SuperdeskApiError.badRequestError(message='Only images can be cropped!')

        # Check if the renditions exists
        if not original.get('renditions'):
            raise SuperdeskApiError.badRequestError(message='Missing renditions!')

        # Check if the original rendition exists
        if not original.get('renditions').get('original'):
            raise SuperdeskApiError.badRequestError(message='Missing original rendition!')

        # Check if the crop name is valid
        crop = self.get_crop_by_name(crop_name)
        crop_data = updates.get('renditions', {}).get(crop_name, {})
        if not crop and 'CropLeft' in crop_data:
            raise SuperdeskApiError.badRequestError(message='Unknown crop name! (name=%s)' % crop_name)

        self._validate_values(crop_data)
        self._validate_poi(original, updates, crop_name)
        self._validate_aspect_ratio(crop, crop_data)

    def _validate_values(self, crop):
        int_fields = ('CropLeft', 'CropTop', 'CropRight', 'CropBottom', 'width', 'height')
        for field in int_fields:
            if field in crop and type(crop[field]) != int:
                raise SuperdeskApiError.badRequestError('Invalid value for %s in renditions' % field)

    def _validate_poi(self, original, updates, crop_name):
        """
        Validate the crop point of interest in the renditions dictionary for the given crop
        :param dict original: original item
        :param dict updates: updated renditions
        """
        renditions = original.get('renditions', {})
        original_image = renditions['original']
        updated_renditions = updates.get('renditions', {})
        if 'poi' in updates:
            if 'x' not in updates['poi'] or 'y' not in updates['poi']:
                del updates['poi']
                return
            poi = updates['poi']
        elif 'poi' not in original:
            return
        else:
            if crop_name not in updated_renditions:
                return
            poi = original['poi']

        crop_data = updated_renditions[crop_name] if crop_name in updated_renditions else renditions[crop_name]
        orig_poi_x = int(original_image['width'] * poi['x'])
        orig_poi_y = int(original_image['height'] * poi['y'])

        if orig_poi_y < crop_data.get('CropTop', 0) \
                or orig_poi_y > crop_data.get('CropBottom', original_image['height']) \
                or orig_poi_x < crop_data.get('CropLeft', 0) \
                or orig_poi_x > crop_data.get('CropRight', original_image['width']):
            raise SuperdeskApiError('Point of interest outside the crop %s limits' % crop_name)

    def _validate_aspect_ratio(self, crop, doc):
        """
        Checks if the aspect ratio is consistent with one in defined in spec
        :param crop: Spec parameters
        :param doc: Posted parameters
        :raises SuperdeskApiError.badRequestError:
        """
        if 'CropLeft' not in doc:
            return

        width = doc['CropRight'] - doc['CropLeft']
        height = doc['CropBottom'] - doc['CropTop']
        if not (crop.get('width') or crop.get('height') or crop.get('ratio')):
            raise SuperdeskApiError.badRequestError(
                message='Crop data are missing. width, height or ratio need to be defined')
        if crop.get('width') and crop.get('height'):
            expected_crop_width = int(crop['width'])
            expected_crop_height = int(crop['height'])
            if width < expected_crop_width or height < expected_crop_height:
                raise SuperdeskApiError.badRequestError(
                    message='Wrong crop size. Minimum crop size is {}x{}.'.format(crop['width'], crop['height']))
                doc_ratio = round(width / height, 1)
                spec_ratio = round(expected_crop_width / expected_crop_height, 1)
                if doc_ratio != spec_ratio:
                    raise SuperdeskApiError.badRequestError(message='Wrong aspect ratio!')
        elif crop.get('ratio'):
            ratio = crop.get('ratio')
            if type(ratio) not in [int, float]:
                ratio = ratio.split(':')
                ratio = int(ratio[0]) / int(ratio[1])
            if abs((width / height) - ratio) > 0.01:
                raise SuperdeskApiError.badRequestError(
                    message='Ratio %s is not respected. We got %f' % (crop.get('ratio'), abs((width / height))))

    def get_crop_by_name(self, crop_name):
        """
        Finds the crop in the list of crops by name
        :param crop_name: Crop name
        :return: Matching crop or None
        """
        if not self.crop_sizes:
            self.crop_sizes = get_resource_service('vocabularies').find_one(req=None, _id='crop_sizes').get('items')

        if not self.crop_sizes:
            raise SuperdeskApiError.badRequestError(message='Crops sizes couldn\'t be loaded!')

        return next((c for c in self.crop_sizes if c.get('name', '').lower() == crop_name.lower()), None)

    def create_crop(self, original, crop_name, crop_data):
        """
        Create a new crop based on the crop co-ordinates
        :param original: Article to add the crop
        :param crop_name: Name of the crop
        :param doc: Crop details
        :raises SuperdeskApiError.badRequestError
        :return dict: modified renditions
        """
        renditions = original.get('renditions', {})
        original_crop = renditions.get(crop_name, {})
        fields = ('CropLeft', 'CropTop', 'CropRight', 'CropBottom')
        crop_created = False
        if any(crop_data.get(name) != original_crop.get(name) for name in fields):
            original_image = renditions.get('original', {})
            original_file = superdesk.app.media.fetch_rendition(original_image)
            if not original_file:
                raise SuperdeskApiError.badRequestError('Original file couldn\'t be found')
            try:
                cropped, out = crop_image(original_file, crop_name, crop_data)
                crop = self.get_crop_by_name(crop_name)
                if not cropped:
                    raise SuperdeskApiError.badRequestError('Saving crop failed.')
                # resize if needed
                if crop.get('width') or crop.get('height'):
                    out, width, height = _resize_image(out, size=(crop.get('width'), crop.get('height')))
                    crop['width'] = width
                    crop['height'] = height
                    out.seek(0)
                renditions[crop_name] = self._save_cropped_image(out, original_image, crop_data)
                crop_created = True
            except SuperdeskApiError:
                raise
            except Exception as ex:
                raise SuperdeskApiError.badRequestError('Generating crop failed: {}'.format(str(ex)))
        return renditions, crop_created

    def _save_cropped_image(self, file_stream, original, doc):
        """
        Saves the cropped image and returns the crop dictionary
        :param file_stream: cropped image stream
        :param original: original rendition
        :param doc: crop data
        :return dict: Crop values
        :raises SuperdeskApiError.internalError
        """
        crop = {}
        try:
            file_name, content_type, metadata = process_file_from_stream(file_stream,
                                                                         content_type=original.get('mimetype'))
            file_stream.seek(0)
            file_id = superdesk.app.media.put(file_stream, filename=file_name,
                                              content_type=content_type,
                                              resource='upload',
                                              metadata=metadata)
            crop['media'] = file_id
            crop['mimetype'] = content_type
            crop['href'] = url_for_media(file_id, content_type)
            crop['CropTop'] = doc.get('CropTop', None)
            crop['CropLeft'] = doc.get('CropLeft', None)
            crop['CropRight'] = doc.get('CropRight', None)
            crop['CropBottom'] = doc.get('CropBottom', None)
            return crop
        except Exception as ex:
            try:
                superdesk.app.media.delete(file_id)
            except:
                pass
            raise SuperdeskApiError.internalError('Generating crop failed: {}'.format(str(ex)))

    def _delete_crop_file(self, file_id):
        """
        Delete the crop file
        :param Object_id file_id: Object_Id of the file.
        """
        try:
            superdesk.app.media.delete(file_id)
        except:
            logger.exception("Crop File cannot be deleted. File_Id {}".format(file_id))

    def create_multiple_crops(self, updates, original):
        """
        Create multiple crops based on the renditions.
        :param dict updates: update item
        :param dict original: original of the updated item
        """
        update_renditions = updates.get('renditions', {})
        if original.get(ITEM_TYPE) == CONTENT_TYPE.PICTURE and update_renditions:
            renditions = original.get('renditions', {})
            original_copy = deepcopy(original)
            for key in update_renditions:
                if self.get_crop_by_name(key):
                    renditions, crop_created = self.create_crop(original_copy, key,
                                                                update_renditions.get(key, {}))
            poi = updates.get('poi', {})
            if poi:
                for crop_name in renditions:
                    self._set_crop_poi(renditions, crop_name, poi)

            updates['renditions'] = renditions

    def _set_crop_poi(self, renditions, crop_name, poi):
        """
        Set the crop point of interest in the renditions dictionary for the given crop
        :param dict renditions: updated renditions
        :param string crop_name: the crop for which to set the poi
        :param dict poi: the point of interest dictionary
        """
        fields = ('CropLeft', 'CropTop', 'CropRight', 'CropBottom')
        if 'x' in poi and 'y' in poi:
            original_image = renditions['original']
            crop_data = renditions[crop_name]
            orig_poi_x = int(original_image['width'] * poi['x'])
            orig_poi_y = int(original_image['height'] * poi['y'])

            if any(name in crop_data for name in fields):
                crop_poi_x = orig_poi_x - crop_data.get('CropLeft', 0)
                crop_poi_y = orig_poi_y - crop_data.get('CropTop', 0)
            else:
                crop_poi_x = int(crop_data.get('width', original_image['width']) * poi['x'])
                crop_poi_y = int(crop_data.get('height', original_image['height']) * poi['y'])
            renditions[crop_name]['poi'] = {'x': crop_poi_x, 'y': crop_poi_y}

    def validate_multiple_crops(self, updates, original):
        """
        Validate crops for the image
        :param dict updates: update item
        :param dict original: original of the updated item
        """
        renditions = updates.get('renditions', {})
        if renditions and original.get(ITEM_TYPE) == CONTENT_TYPE.PICTURE:
            for key in renditions:
                self.validate_crop(original, updates, key)

    def delete_replaced_crop_files(self, updates, original):
        """
        Delete the replaced crop files.
        :param dict updates: update item
        :param dict original: original of the updated item
        """
        update_renditions = updates.get('renditions', {})
        if original.get(ITEM_TYPE) == CONTENT_TYPE.PICTURE and update_renditions:
            renditions = original.get('renditions', {})
            for key in update_renditions:
                if self.get_crop_by_name(key) and \
                        update_renditions.get(key, {}).get('media') != \
                        renditions.get(key, {}).get('media'):
                    self._delete_crop_file(renditions.get(key, {}).get('media'))
