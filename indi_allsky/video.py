import os
import io
import time
from datetime import datetime
from pathlib import Path
import subprocess
import tempfile
import fcntl
import errno

from .keogram import KeogramGenerator
from .db import IndiAllSkyDb

from sqlalchemy.orm.exc import NoResultFound

from multiprocessing import Process
#from threading import Thread
import multiprocessing

logger = multiprocessing.get_logger()


class VideoProcessWorker(Process):

    video_lockfile = '/tmp/timelapse_video.lock'


    def __init__(self, idx, config, video_q, upload_q):
        super(VideoProcessWorker, self).__init__()

        #self.threadID = idx
        self.name = 'VideoProcessWorker{0:03d}'.format(idx)

        self.config = config
        self.video_q = video_q
        self.upload_q = upload_q

        self._db = IndiAllSkyDb(self.config)

        self.f_lock = None


    def run(self):
        while True:
            v_dict = self.video_q.get()

            if v_dict.get('stop'):
                return

            try:
                self._getLock()  # get lock to prevent multiple videos from being concurrently generated
            except BlockingIOError as e:
                if e.errno == errno.EAGAIN:
                    logger.error('Failed to get exclusive lock: %s', str(e))
                    return


            timespec = v_dict['timespec']
            img_folder = v_dict['img_folder']
            timeofday = v_dict['timeofday']
            video = v_dict.get('video', True)
            keogram = v_dict.get('keogram', True)


            if not img_folder.exists():
                logger.error('Image folder does not exist: %s', img_folder)
                continue


            if video:
                self.generateVideo(timespec, img_folder, timeofday)


            if keogram:
                self.generateKeogram(timespec, img_folder, timeofday)


            self._releaseLock()



    def generateVideo(self, timespec, img_folder, timeofday):
        from .db import IndiAllSkyDbImageTable
        from .db import IndiAllSkyDbVideoTable

        dbsession = self._db.session


        try:
            d_datetime = datetime.strptime(timespec, '%Y%m%d')
            d_daydate = d_datetime.date()
        except ValueError:
            logger.error('Invalid time spec')
            return


        if timeofday == 'night':
            night = True
        else:
            night = False


        video_file = img_folder.parent.joinpath('allsky-timelapse-{0:s}-{1:s}.mp4'.format(timespec, timeofday))

        if video_file.exists():
            logger.warning('Video is already generated: %s', video_file)
            return


        try:
            # delete old video entry if it exists
            video_entry = dbsession.query(IndiAllSkyDbVideoTable)\
                .filter(IndiAllSkyDbVideoTable.filename == str(video_file))\
                .one()

            logger.warning('Removing orphaned video db entry')
            dbsession.delete(video_entry)
            dbsession.commit()
        except NoResultFound:
            pass


        seqfolder = tempfile.TemporaryDirectory()
        p_seqfolder = Path(seqfolder.name)


        # find all files
        timelapse_files_entries = dbsession.query(IndiAllSkyDbImageTable)\
            .filter(IndiAllSkyDbImageTable.daydate == d_daydate)\
            .filter(IndiAllSkyDbImageTable.night == night)\
            .order_by(IndiAllSkyDbImageTable.datetime.asc())


        logger.info('Found %d images for timelapse', timelapse_files_entries.count())

        timelapse_files = list()
        for entry in timelapse_files_entries:
            p_entry = Path(entry.filename)

            if not p_entry.exists():
                logger.error('File not found: %s', p_entry)
                continue

            if p_entry.stat().st_size == 0:
                continue

            timelapse_files.append(p_entry)


        for i, f in enumerate(timelapse_files):
            p_symlink = p_seqfolder.joinpath('{0:05d}.{1:s}'.format(i, self.config['IMAGE_FILE_TYPE']))
            p_symlink.symlink_to(f)


        start = time.time()

        cmd = [
            'ffmpeg',
            '-y',
            '-f', 'image2',
            '-r', '{0:d}'.format(self.config['FFMPEG_FRAMERATE']),
            '-i', '{0:s}/%05d.{1:s}'.format(str(p_seqfolder), self.config['IMAGE_FILE_TYPE']),
            '-vcodec', 'libx264',
            '-b:v', '{0:s}'.format(self.config['FFMPEG_BITRATE']),
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '{0:s}'.format(str(video_file)),
        ]

        ffmpeg_subproc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=lambda: os.nice(19),
        )

        elapsed_s = time.time() - start
        logger.info('Timelapse generated in %0.4f s', elapsed_s)

        logger.info('FFMPEG output: %s', ffmpeg_subproc.stdout)

        # delete all existing symlinks and sequence folder
        seqfolder.cleanup()


        video_entry = self._db.addVideo(
            video_file,
            timeofday,
        )

        ### Upload ###
        self.uploadVideo(video_file)

        self._db.addUploadedFlag(video_entry)



    def uploadVideo(self, video_file):
        ### Upload video
        if not self.config['FILETRANSFER']['UPLOAD_VIDEO']:
            logger.warning('Video uploading disabled')
            return

        remote_path = Path(self.config['FILETRANSFER']['REMOTE_VIDEO_FOLDER'])
        remote_file = remote_path.joinpath(video_file.name)

        # tell worker to upload file
        self.upload_q.put({
            'local_file' : video_file,
            'remote_file' : remote_file,
        })


    def generateKeogram(self, timespec, img_folder, timeofday):
        from .db import IndiAllSkyDbImageTable
        from .db import IndiAllSkyDbKeogramTable

        dbsession = self._db.session


        try:
            d_datetime = datetime.strptime(timespec, '%Y%m%d')
            d_daydate = d_datetime.date()
        except ValueError:
            logger.error('Invalid time spec')
            return


        if timeofday == 'night':
            night = True
        else:
            night = False



        keogram_file = img_folder.parent.joinpath('allsky-keogram-{0:s}-{1:s}.jpg'.format(timespec, timeofday))

        if keogram_file.exists():
            logger.warning('Keogram is already generated: %s', keogram_file)
            return


        try:
            # delete old keogram entry if it exists
            keogram_entry = dbsession.query(IndiAllSkyDbKeogramTable)\
                .filter(IndiAllSkyDbKeogramTable.filename == str(keogram_file))\
                .one()

            logger.warning('Removing orphaned keogram db entry')
            dbsession.delete(keogram_entry)
            dbsession.commit()
        except NoResultFound:
            pass


        # find all files
        keogram_files_entries = dbsession.query(IndiAllSkyDbImageTable)\
            .filter(IndiAllSkyDbImageTable.daydate == d_daydate)\
            .filter(IndiAllSkyDbImageTable.night == night)\
            .order_by(IndiAllSkyDbImageTable.datetime.asc())


        logger.info('Found %d images for keogram', keogram_files_entries.count())


        keogram_files = list()
        for entry in keogram_files_entries:
            p_entry = Path(entry.filename)

            if not p_entry.exists():
                logger.error('File not found: %s', p_entry)
                continue

            if p_entry.stat().st_size == 0:
                continue

            keogram_files.append(p_entry)



        kg = KeogramGenerator(self.config, keogram_files)
        kg.angle = self.config['KEOGRAM_ANGLE']
        kg.h_scale_factor = self.config['KEOGRAM_H_SCALE']
        kg.v_scale_factor = self.config['KEOGRAM_V_SCALE']
        kg.generate(keogram_file)

        keogram_entry = self._db.addKeogram(
            keogram_file,
            timeofday,
        )

        self.uploadKeogram(keogram_file)

        self._db.addUploadedFlag(keogram_entry)


    def uploadKeogram(self, keogram_file):
        ### Upload video
        if not self.config['FILETRANSFER']['UPLOAD_KEOGRAM']:
            logger.warning('Keogram uploading disabled')
            return

        remote_path = Path(self.config['FILETRANSFER']['REMOTE_KEOGRAM_FOLDER'])
        remote_file = remote_path.joinpath(keogram_file.name)

        # tell worker to upload file
        self.upload_q.put({
            'local_file' : keogram_file,
            'remote_file' : remote_file,
        })


    def getFolderFilesByExt(self, folder, file_list, extension_list=None):
        if not extension_list:
            extension_list = [self.config['IMAGE_FILE_TYPE']]

        logger.info('Searching for image files in %s', folder)

        dot_extension_list = ['.{0:s}'.format(e) for e in extension_list]

        for item in Path(folder).iterdir():
            if item.is_file() and item.suffix in dot_extension_list:
                file_list.append(item)
            elif item.is_dir():
                self.getFolderFilesByExt(item, file_list, extension_list=extension_list)  # recursion


    def _getLock(self):
        logger.info('Get exclusive lock to generate video')
        lock_p = Path(self.video_lockfile)

        if not lock_p.is_file():
            f_lock = io.open(str(lock_p), 'w+')
            f_lock.close()
            lock_p.chmod(0o644)

        self.f_lock = io.open(str(lock_p), 'w+')
        fcntl.flock(self.f_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Exclusive, non-blocking lock


    def _releaseLock(self):
        logger.info('Release exclusive lock')
        fcntl.flock(self.f_lock, fcntl.LOCK_UN)
        self.f_lock.close()

