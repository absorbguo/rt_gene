#!/usr/bin/env python

"""
Convolutional Neural Network (CNN) for eye gaze estimation
@Tobias Fischer (t.fischer@imperial.ac.uk)
@Hyung Jin Chang (hj.chang@imperial.ac.uk)
@Kevin Cortacero <cortacero.k31130@gmail.com>
Licensed under Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode)
"""

from __future__ import print_function, division, absolute_import

import os
import numpy as np
from tqdm import tqdm

import rospkg
import rospy
from sensor_msgs.msg import Image
from cv_bridge import CvBridge

# noinspection PyUnresolvedReferences
import rt_gene.gaze_tools as gaze_tools
from tf import TransformBroadcaster, TransformListener
import tf.transformations
import collections

from rt_gene.subject_ros_bridge import SubjectListBridge
from rt_gene.msg import MSG_SubjectImagesList

from rt_gene.estimate_gaze_base import GazeEstimatorBase


class GazeEstimatorROS(GazeEstimatorBase):
    def __init__(self, device_id_gaze, model_files):
        super(GazeEstimatorROS, self).__init__(device_id_gaze, model_files)
        self.bridge = CvBridge()
        self.subjects_bridge = SubjectListBridge()

        self.tf_broadcaster = TransformBroadcaster()
        self.tf_listener = TransformListener()

        self.tf_prefix = rospy.get_param("~tf_prefix", "gaze")
        self.headpose_frame = self.tf_prefix + "/head_pose_estimated"
        self.rgb_frame_id_ros = rospy.get_param("~rgb_frame_id_ros", "/kinect2_nonrotated_link")

        self.image_subscriber = rospy.Subscriber('/subjects/images', MSG_SubjectImagesList, self.image_callback, queue_size=1, buff_size=10000000)
        self.subjects_gaze_img = rospy.Publisher('/subjects/gazeimages', Image, queue_size=3)

        self.average_weights = np.array([0.1, 0.125, 0.175, 0.2, 0.4])
        self.gaze_buffer_c = {}
        self.time_last = rospy.Time.now()

    def publish_image(self, image, image_publisher, timestamp):
        """This image publishes the `image` to the `image_publisher` with the given `timestamp`."""
        image_ros = self.bridge.cv2_to_imgmsg(image, "rgb8")
        image_ros.header.stamp = timestamp
        image_publisher.publish(image_ros)

    def compute_eye_gaze_estimation(self, subject_id, timestamp, input_r, input_l):
        """
        subject_id : integer,  id of the subject
        input_x    : cv_image, input image of x eye
        (phi_x)    : double,   phi angle estimated using pupil detection
        (theta_x)  : double,   theta angle estimated using pupil detection
        """
        try:
            lct = self.tf_listener.getLatestCommonTime(self.rgb_frame_id_ros, self.headpose_frame + str(subject_id))
            if (timestamp - lct).to_sec() < 0.25:
                # tqdm.write('Time diff: ' + str((timestamp - lct).to_sec()))

                (trans_head, rot_head) = self.tf_listener.lookupTransform(self.rgb_frame_id_ros, self.headpose_frame + str(subject_id), lct)
                euler_angles_head = gaze_tools.limit_yaw(rot_head)

                phi_head, theta_head = gaze_tools.get_phi_theta_from_euler(euler_angles_head)
                print('euler_angles_head estimate_gaze: {}'.format(euler_angles_head))
            else:
                tqdm.write('Too big time diff for head pose, do not estimate gaze!' + str((timestamp - lct).to_sec()))
                return

            est_gaze_c = self.estimate_gaze_twoeyes(input_l, input_r, np.array([theta_head, phi_head]))

            self.gaze_buffer_c[subject_id].append(est_gaze_c)

            if len(self.average_weights) == len(self.gaze_buffer_c[subject_id]):
                est_gaze_c_med = np.average(np.array(self.gaze_buffer_c[subject_id]), axis=0, weights=self.average_weights)
                self.publish_gaze(est_gaze_c_med, timestamp, subject_id)
                time_total = (rospy.Time.now() - timestamp).to_sec()
                tqdm.write('est_gaze_c: {gaze} (fps: {fps:.1f}, latency: {time:.2f}s)'.format(gaze=est_gaze_c_med, fps=1. / (rospy.Time.now() - self.time_last).to_sec(), time=time_total))
                self.time_last = rospy.Time.now()
                return est_gaze_c_med

        except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException, tf.Exception) as tf_e:
            print(tf_e)
        except rospy.ROSException as ros_e:
            if str(ros_e) == "publish() to a closed topic":
                print("See ya")
        return None

    def image_callback(self, subject_image_list):
        """This method is called whenever new input arrives. The input is first converted in a format suitable
        for the gaze estimation network (see :meth:`input_from_image`), then the gaze is estimated (see
        :meth:`estimate_gaze`. The estimated gaze is overlaid on the input image (see :meth:`visualize_eye_result`),
        and this image is published along with the estimated gaze vector (see :meth:`publish_image` and
        :func:`publish_gaze`)"""
        timestamp = subject_image_list.header.stamp
        subjects_gaze_img = None

        subjects_dict = self.subjects_bridge.msg_to_images(subject_image_list)
        for subject_id, s in subjects_dict.items():
            if subject_id not in self.gaze_buffer_c.keys():
                self.gaze_buffer_c[subject_id] = collections.deque(maxlen=5)

            input_r = self.input_from_image(s.right)
            input_l = self.input_from_image(s.left)
            gaze_est = self.compute_eye_gaze_estimation(subject_id, timestamp, input_r, input_l)

            if gaze_est is not None:
                r_gaze_img = self.visualize_eye_result(s.right, gaze_est)
                l_gaze_img = self.visualize_eye_result(s.left, gaze_est)
                s_gaze_img = np.concatenate((r_gaze_img, l_gaze_img), axis=1)
                if subjects_gaze_img is None:
                    subjects_gaze_img = s_gaze_img
                else:
                    subjects_gaze_img = np.concatenate((subjects_gaze_img, s_gaze_img), axis=0)

        if subjects_gaze_img is not None:
            gaze_img_msg = self.bridge.cv2_to_imgmsg(subjects_gaze_img.astype(np.uint8), "bgr8")
            self.subjects_gaze_img.publish(gaze_img_msg)

    def publish_gaze(self, est_gaze, msg_stamp, subject_id):
        """Publish the gaze vector as a PointStamped."""
        theta_gaze = est_gaze[0]
        phi_gaze = est_gaze[1]
        euler_angle_gaze = gaze_tools.get_euler_from_phi_theta(phi_gaze, theta_gaze)
        quaternion_gaze = tf.transformations.quaternion_from_euler(*euler_angle_gaze)
        self.tf_broadcaster.sendTransform((0, 0, 0.05),  # publish it 5cm above the head pose's origin (nose tip)
                                          quaternion_gaze, msg_stamp, self.tf_prefix + "/world_gaze" + str(subject_id), self.headpose_frame + str(subject_id))


if __name__ == '__main__':
    try:
        rospy.init_node('estimate_gaze')
        gaze_estimator = GazeEstimatorROS(rospy.get_param("~device_id_gazeestimation", default="/gpu:0"),
                                          [os.path.join(rospkg.RosPack().get_path('rt_gene'), model_file) for model_file in rospy.get_param("~model_files")])
        rospy.spin()
    except rospy.exceptions.ROSInterruptException:
        print("See ya")
    except rospy.ROSException as e:
        if str(e) == "publish() to a closed topic":
            print("See ya")
        else:
            raise e
    except KeyboardInterrupt:
        print("Shutting down")
