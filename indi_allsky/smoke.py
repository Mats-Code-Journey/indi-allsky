
import time
from datetime import datetime
from datetime import timedelta  # noqa: F401
from collections import OrderedDict
import socket
import ssl
import requests
from lxml import etree
import shapely
import logging

from . import constants

from .flask import db
from .flask.miscDb import miscDb


logger = logging.getLogger('indi_allsky')


class IndiAllskySmokeUpdate(object):

    hms_kml_base_url = 'https://satepsanone.nesdis.noaa.gov/pub/FIRE/web/HMS/Smoke_Polygons/KML/{now:%Y}/{now:%m}/hms_smoke{now:%Y}{now:%m}{now:%d}.kml'


    # folder name, rating
    hms_kml_folders = OrderedDict({
        # check from light to heavy, in order
        'Smoke (Light)'  : 'light',
        'Smoke (Medium)' : 'medium',
        'Smoke (Heavy)'  : 'heavy',
    })


    def __init__(self, config):
        self.config = config

        self._miscDb = miscDb(self.config)

        self.hms_kml_data = None


    def update(self, camera):
        latitude = camera.latitude
        longitude = camera.longitude


        if camera.data:
            camera_data = dict(camera.data)
        else:
            camera_data = dict()


        if latitude > 0 and longitude < 0:
            # HMS data is only good for north western hemisphere
            smoke_rating = self.update_na_hms(camera)

        else:
            # all other regions report no data
            smoke_rating = 'no data'


        if smoke_rating:
            logger.info('Smoke rating: %s', str(smoke_rating))

            camera_data['SMOKE_RATING'] = constants.SMOKE_STR_MAP[smoke_rating]
            camera_data['SMOKE_DATA_TS'] = int(time.time())
            camera.data = camera_data
            db.session.commit()
        else:
            logger.warning('Smoke data not updated')


    def update_na_hms(self, camera):
        # this pulls data from NOAA Hazard Mapping System
        # https://www.ospo.noaa.gov/Products/land/hms.html

        now = datetime.now()
        #now = datetime.now() - timedelta(days=1)  # testing

        hms_kml_url = self.hms_kml_base_url.format(**{'now' : now})


        # allow data to be reused
        if not self.hms_kml_data:
            try:
                self.hms_kml_data = self.download_kml(hms_kml_url)
            except socket.gaierror as e:
                logger.error('Name resolution error: %s', str(e))
                self.hms_kml_data = None
            except socket.timeout as e:
                logger.error('Timeout error: %s', str(e))
                self.hms_kml_data = None
            except requests.exceptions.ConnectTimeout as e:
                logger.error('Connection timeout: %s', str(e))
                self.hms_kml_data = None
            except requests.exceptions.ConnectionError as e:
                logger.error('Connection error: %s', str(e))
                self.hms_kml_data = None
            except ssl.SSLCertVerificationError as e:
                logger.error('Certificate error: %s', str(e))
                self.hms_kml_data = None
            except requests.exceptions.SSLError as e:
                logger.error('Certificate error: %s', str(e))
                self.hms_kml_data = None


        latitude = camera.latitude
        longitude = camera.longitude


        if self.hms_kml_data:
            try:
                xml_root = etree.fromstring(self.hms_kml_data)
            except etree.XMLSyntaxError as e:
                logger.error('Unable to parse XML: %s', str(e))
                return ''
            except ValueError as e:
                logger.error('Unable to parse XML: %s', str(e))
                return ''


            # look for a 1 square degree area (smoke within ~35 miles)
            location_area = shapely.Polygon((
                (float(longitude) - 0.5, float(latitude) - 0.5),
                (float(longitude) + 0.5, float(latitude) - 0.5),
                (float(longitude) + 0.5, float(latitude) + 0.5),
                (float(longitude) - 0.5, float(latitude) + 0.5),
            ))



            NS = {
                "kml" : "http://www.opengis.net/kml/2.2",
            }


            smoke_rating = 'Clear'  # no matches should mean clear

            found_kml_folders = False
            for folder, rating in self.hms_kml_folders.items():
                p = ".//kml:Folder[contains(., '{0:s}')]".format(folder)
                #logger.info('Folder: %s', p)
                e_folder = xml_root.xpath(p, namespaces=NS)


                if not e_folder:
                    logger.error('Folder not found: %s', folder)
                    continue

                found_kml_folders = True


                for e_placemark in e_folder[0].xpath('.//kml:Placemark', namespaces=NS):
                    for e_polygon in e_placemark.xpath('.//kml:Polygon', namespaces=NS):
                        e_coord = e_polygon.find(".//kml:coordinates", namespaces=NS)
                        #logger.info('   %s', pformat(e_coord.text))

                        coord_list = list()
                        for line in e_coord.text.splitlines():
                            line = line.strip()

                            if not line:
                                continue

                            #logger.info('line: %s', pformat(line))
                            p_long, p_lat, p_z = line.split(',')
                            coord_list.append((float(p_long), float(p_lat)))

                        smoke_polygon = shapely.Polygon(coord_list)

                        if location_area.intersects(smoke_polygon):
                            smoke_rating = rating


            if not found_kml_folders:
                # without folders, there was no data to match
                logger.error('No folders in KML')
                return ''


            return smoke_rating


        logger.error('No KML data')
        return ''


    def download_kml(self, url):
        logger.warning('Downloading %s', url)
        r = requests.get(url, allow_redirects=True, verify=True, timeout=15.0)

        if r.status_code >= 400:
            logger.error('URL returned %d', r.status_code)
            return None

        return r.text.encode()

