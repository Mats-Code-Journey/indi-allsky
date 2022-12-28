import time
import numpy
import cv2
import astroalign
import logging

logger = logging.getLogger('indi_allsky')


class IndiAllskyStacker(object):

    def __init__(self, config, bin_v, mask=None):
        self.config = config
        self.bin_v = bin_v

        self._sqm_mask = mask


    def mean(self, *args, **kwargs):
        # alias for average
        return self.average(*args, **kwargs)


    def average(self, stack_data_list, numpy_type):
        mean_image = numpy.mean(stack_data_list, axis=0)
        return numpy.floor(mean_image).astype(numpy_type)  # no floats


    def maximum(self, stack_data_list, numpy_type):
        image_max = stack_data_list[0]  # start with first image

        # compare with remaining images
        for i in stack_data_list[1:]:
            image_max = numpy.maximum(image_max, i)

        return image_max

    def minimum(self, stack_data_list, numpy_type):
        image_min = stack_data_list[0]  # start with first image

        # compare with remaining images
        for i in stack_data_list[1:]:
            image_min = numpy.minimum(image_min, i)

        return image_min


    def register(self, stack_i_ref_list):
        # first image is the reference
        reference_i_ref = stack_i_ref_list[0]


        if isinstance(self._sqm_mask, type(None)):
            # This only needs to be done once if a mask is not provided
            self._generateSqmMask(reference_i_ref['hdulist'][0].data)


        reg_data_list = [reference_i_ref['hdulist'][0].data]  # add target to final list

        #reference_masked = self._crop(reference_i_ref['hdulist'][0].data)
        reference_masked = cv2.bitwise_and(reference_i_ref['hdulist'][0].data, reference_i_ref['hdulist'][0].data, mask=self._sqm_mask)

        reg_start = time.time()

        for i_ref in stack_i_ref_list[1:]:
            #i_masked = self._crop(i_ref['hdulist'][0].data)
            i_masked = cv2.bitwise_and(i_ref['hdulist'][0].data, i_ref['hdulist'][0].data, mask=self._sqm_mask)

            # detection_sigma default = 5
            # max_control_points default = 50
            # min_area default = 5

            try:
                ### Find transform using a crop of the image
                transform, (source_list, target_list) = astroalign.find_transform(
                    i_masked,
                    reference_masked,
                    detection_sigma=5,
                    max_control_points=150,
                    min_area=15,
                )

                logger.info(
                    'Registration Matches: %d, Rotation: %0.6f, Translation: (%0.6f, %0.6f), Scale: %0.6f',
                    len(target_list),
                    transform.rotation,
                    transform.translation[0], transform.translation[1],
                    transform.scale,
                )

                reg_data, footprint = astroalign.apply_transform(
                    transform,
                    i_ref['hdulist'][0],
                    reference_i_ref['hdulist'][0],
                )

                ### Register full image
                #reg_data, footprint = astroalign.register(
                #    i_ref['hdulist'][0],
                #    reference_i_ref['hdulist'][0],
                #    detection_sigma=5,
                #    max_control_points=150,
                #    min_area=15,
                #)
            except astroalign.MaxIterError as e:
                logger.error('Image registration failure: %s', str(e))
                continue
            except ValueError as e:
                logger.error('Image registration failure: %s', str(e))
                continue

            reg_data_list.append(reg_data)


        reg_elapsed_s = time.time() - reg_start
        logger.info('Registered %d+1 images in %0.4f s', len(stack_i_ref_list) - 1, reg_elapsed_s)  # reference image is not aligned

        return reg_data_list


    def _crop(self, image):
        image_height, image_width = image.shape[:2]

        x1 = int((image_width / 2) - (image_width / 3))
        y1 = int((image_height / 2) - (image_height / 3))
        x2 = int((image_width / 2) + (image_width / 3))
        y2 = int((image_height / 2) + (image_height / 3))


        return image[
            y1:y2,
            x1:x2,
        ]


    def _generateSqmMask(self, img):
        logger.info('Generating mask based on SQM_ROI')

        image_height, image_width = img.shape[:2]

        # create a black background
        mask = numpy.zeros((image_height, image_width), dtype=numpy.uint8)

        sqm_roi = self.config.get('SQM_ROI', [])

        try:
            x1 = int(sqm_roi[0] / self.bin_v.value)
            y1 = int(sqm_roi[1] / self.bin_v.value)
            x2 = int(sqm_roi[2] / self.bin_v.value)
            y2 = int(sqm_roi[3] / self.bin_v.value)
        except IndexError:
            logger.warning('Using central ROI for registration')
            x1 = int((image_width / 2) - (image_width / 3))
            y1 = int((image_height / 2) - (image_height / 3))
            x2 = int((image_width / 2) + (image_width / 3))
            y2 = int((image_height / 2) + (image_height / 3))

        # The white area is what we keep
        cv2.rectangle(
            img=mask,
            pt1=(x1, y1),
            pt2=(x2, y2),
            color=(255),  # mono
            thickness=cv2.FILLED,
        )

        self._sqm_mask = mask


