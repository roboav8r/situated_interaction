#!/usr/bin/env python3

# import time
from string import ascii_lowercase as alc

import numpy as np

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.node import Node
from rclpy.time import Time, Duration

import gtsam

from foxglove_msgs.msg import SceneUpdate, SceneEntity, TextPrimitive
from tracking_msgs.msg import Tracks3D

from situated_hri_interfaces.msg import Auth, Comm, Comms, Identity, CategoricalDistribution
from situated_hri_interfaces.srv import ObjectVisRec

from situated_interaction.utils import pmf_to_spec, normalize_vector
from situated_interaction.datatypes import DiscreteVariable

class SemanticObject():
    def __init__(self, msg, params):

        self.track_id = msg.track_id
        self.class_string = msg.class_string
        self.stamp = msg.time_updated
        self.identity = "Unknown"
        self.auth = False
        self.comms = {}
        self.pos_x = msg.pose.pose.position.x
        self.pos_y = msg.pose.pose.position.y
        self.pos_z = msg.pose.pose.position.z
        
        if msg.image_available == True:
            self.new_image_available = True
            self.image = msg.image
        else:
            self.new_image_available = False
            self.image = None
        
        self.vis_rec_complete = False

        # Initialize attributes, states, and obs models
        symbol_idx = 0
        self.attributes = {}
        for att in params['attributes']:
            self.attributes[att] = DiscreteVariable(att, 'attribute', params['attributes'][att]['update_method'], params['attributes'][att]['update_threshold'], 
                                                        Time.from_msg(self.stamp), params['attributes'][att]['labels'], alc[symbol_idx], 
                                                        self.track_id, 100, params['attributes'][att]['probs'], 
                                                        pmf_to_spec(params['attributes'][att]['sensor_model_array']), params['upper_prob_limit'], params['lower_prob_limit'])
            symbol_idx+=1

        self.states = {}
        for state in params['states']:

            self.states[state] = DiscreteVariable(state, 'state', params['states'][state]['update_method'], params['states'][state]['update_threshold'], 
                                            Time.from_msg(self.stamp), params['states'][state]['labels'], alc[symbol_idx], 
                                            self.track_id, 100, params['states'][state]['probs'], 
                                            pmf_to_spec(params['states'][state]['sensor_model_array']), params['upper_prob_limit'], params['lower_prob_limit'])
            symbol_idx+=1   

        # self.state_timeout = params['state_timeout']


    # def update(self, ar_msg, type):
    #     if type=='authentication':
    #         if ar_msg.authenticated:
    #             self.auth = True

    #     if type=='communication':
    #         # Increase weight for existing comms in incoming comms
    #         temp_comms = self.comms # dictionary of comm dictionaries
    #         new_comms = ar_msg # list of [situated_hri_interfaces/Comm]
    #         self.comms = {}

    #         # Handle incoming comms
    #         for new_comm in new_comms:

    #             # Incoming message word matches an existing word
    #             if new_comm.comm in temp_comms.keys():

    #                 # Fuse the two and update confidence value to self.comms dict, use parallel addition
    #                 self.comms[new_comm.comm] = 1 - (1 - new_comm.conf)*(1 - temp_comms[new_comm.comm])/((1 - new_comm.conf) + (1 - temp_comms[new_comm.comm]))

    #                 # Remove word from temp_comms
    #                 del temp_comms[new_comm.comm]

    #             elif new_comm.conf > self.det_thresh: # Create new word
    #                 self.comms[new_comm.comm] = new_comm.conf

    #         # Now, find unmatched words in last comm step and reduce confidence
    #         for word in temp_comms.keys():

    #             new_score = temp_comms[word] - self.score_decay
    #             if new_score > self.del_thresh: # Only publish if above delete threshold
    #                 self.comms[word] = new_score

    #     if type=='identity':
    #         self.identity = ar_msg.identity


class SemanticTrackerNode(Node):
    def __init__(self):
        super().__init__('semantic_tracker_node')

        sub_timer_cb_group = MutuallyExclusiveCallbackGroup()
        
        # Subscribe to object tracks
        self.subscription_tracks = self.create_subscription(
            Tracks3D,
            'tracks',
            self.listener_callback_tracks,
            10, callback_group=sub_timer_cb_group)
        self.subscription_tracks  # prevent unused variable warning

        # Define pubs
        self.semantic_scene_pub = self.create_publisher(
            SceneUpdate,
            'semantic_scene',
            10)
        
        # Create timer
        self.declare_parameter('loop_time_sec', rclpy.Parameter.Type.DOUBLE)
        self.loop_time_sec = self.get_parameter('loop_time_sec').get_parameter_value().double_value
        self.timer = self.create_timer(self.loop_time_sec, self.timer_callback, callback_group=sub_timer_cb_group)
        self.service_timeout = .25

        # Create service client
        client_cb_group = ReentrantCallbackGroup()
        self.clip_client = self.create_client(ObjectVisRec, 'clip_object_rec',callback_group=client_cb_group)
        while not self.clip_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info('service not available, waiting again...')
        self.clip_req = ObjectVisRec.Request()

        # self.subscription_auth = self.create_subscription(
        #     Auth,
        #     'authentication',
        #     self.listener_callback_auth,
        #     10)
        # self.subscription_auth  # prevent unused variable warning

        # self.subscription_id = self.create_subscription(
        #     Identity,
        #     'identification',
        #     self.listener_callback_identity,
        #     10)
        # self.subscription_id  # prevent unused variable warning

        # self.subscription_comms = self.create_subscription(
        #     Comms,
        #     'communications',
        #     self.listener_callback_comms,
        #     10)
        # self.subscription_comms  # prevent unused variable warning

        # Define member variables
        self.semantic_objects = {}
        self.tracks_msg = Tracks3D()
        self.scene_out_msg = SceneUpdate()

        # Generate object att/state variable dictionary
        self.object_params = {}
        self.declare_parameter('objects_of_interest', rclpy.Parameter.Type.STRING_ARRAY)
        self.declare_parameter('upper_prob_limit', rclpy.Parameter.Type.DOUBLE)
        self.declare_parameter('lower_prob_limit', rclpy.Parameter.Type.DOUBLE)
        self.objects_of_interest = self.get_parameter('objects_of_interest').get_parameter_value().string_array_value
        self.upper_prob_limit = self.get_parameter('upper_prob_limit').get_parameter_value().double_value
        self.lower_prob_limit = self.get_parameter('lower_prob_limit').get_parameter_value().double_value

        for obj in self.objects_of_interest:

            self.object_params[obj] = {}

            self.object_params[obj]['upper_prob_limit'] = self.upper_prob_limit
            self.object_params[obj]['lower_prob_limit'] = self.lower_prob_limit

            # self.declare_parameter(obj + '.state_timeout', rclpy.Parameter.Type.DOUBLE)
            # self.object_params[obj]['state_timeout'] = self.get_parameter(obj + '.state_timeout').get_parameter_value().double_value

            self.object_params[obj]['attributes'] = {}
            self.declare_parameter(obj + '.attributes.variables', rclpy.Parameter.Type.STRING_ARRAY)
            att_vars = self.get_parameter(obj + '.attributes.variables').get_parameter_value().string_array_value

            for att_var in att_vars:
                self.declare_parameter(obj + '.attributes.' + att_var + '.labels', rclpy.Parameter.Type.STRING_ARRAY)
                self.declare_parameter(obj + '.attributes.' + att_var + '.probs', rclpy.Parameter.Type.DOUBLE_ARRAY)
                self.declare_parameter(obj + '.attributes.' + att_var + '.sensor_model_coeffs', rclpy.Parameter.Type.DOUBLE_ARRAY)
                self.declare_parameter(obj + '.attributes.' + att_var + '.update_method', rclpy.Parameter.Type.STRING)
                self.declare_parameter(obj + '.attributes.' + att_var + '.update_threshold', rclpy.Parameter.Type.DOUBLE)

                self.object_params[obj]['attributes'][att_var] = {}
                self.object_params[obj]['attributes'][att_var]['labels'] = self.get_parameter(obj + '.attributes.' + att_var + '.labels').get_parameter_value().string_array_value
                self.object_params[obj]['attributes'][att_var]['probs'] = self.get_parameter(obj + '.attributes.' + att_var + '.probs').get_parameter_value().double_array_value
                self.object_params[obj]['attributes'][att_var]['sensor_model_coeffs'] = self.get_parameter(obj + '.attributes.' + att_var + '.sensor_model_coeffs').get_parameter_value().double_array_value
                self.object_params[obj]['attributes'][att_var]['sensor_model_array'] = np.array(self.object_params[obj]['attributes'][att_var]['sensor_model_coeffs']).reshape(-1,len(self.object_params[obj]['attributes'][att_var]['labels']))
                self.object_params[obj]['attributes'][att_var]['update_method'] = self.get_parameter(obj + '.attributes.' + att_var + '.update_method').get_parameter_value().string_value
                self.object_params[obj]['attributes'][att_var]['update_threshold'] = self.get_parameter(obj + '.attributes.' + att_var + '.update_threshold').get_parameter_value().double_value

            self.object_params[obj]['states'] = {}
            self.declare_parameter(obj + '.states.variables', rclpy.Parameter.Type.STRING_ARRAY)
            state_vars = self.get_parameter(obj + '.states.variables').get_parameter_value().string_array_value

            for state_var in state_vars:
                self.declare_parameter(obj + '.states.' + state_var + '.labels', rclpy.Parameter.Type.STRING_ARRAY)
                self.declare_parameter(obj + '.states.' + state_var + '.probs', rclpy.Parameter.Type.DOUBLE_ARRAY)
                self.declare_parameter(obj + '.states.' + state_var + '.sensor_model_coeffs', rclpy.Parameter.Type.DOUBLE_ARRAY)
                self.declare_parameter(obj + '.states.' + state_var + '.update_method', rclpy.Parameter.Type.STRING)
                self.declare_parameter(obj + '.states.' + state_var + '.update_threshold', rclpy.Parameter.Type.DOUBLE)

                self.object_params[obj]['states'][state_var] = {}
                self.object_params[obj]['states'][state_var]['labels'] = self.get_parameter(obj + '.states.' + state_var + '.labels').get_parameter_value().string_array_value
                self.object_params[obj]['states'][state_var]['probs'] = self.get_parameter(obj + '.states.' + state_var + '.probs').get_parameter_value().double_array_value
                self.object_params[obj]['states'][state_var]['sensor_model_coeffs'] = self.get_parameter(obj + '.states.' + state_var + '.sensor_model_coeffs').get_parameter_value().double_array_value
                self.object_params[obj]['states'][state_var]['sensor_model_array'] = np.array(self.object_params[obj]['states'][state_var]['sensor_model_coeffs']).reshape(-1,len(self.object_params[obj]['states'][state_var]['labels']))
                self.object_params[obj]['states'][state_var]['update_method'] = self.get_parameter(obj + '.states.' + state_var + '.update_method').get_parameter_value().string_value
                self.object_params[obj]['states'][state_var]['update_threshold'] = self.get_parameter(obj + '.states.' + state_var + '.update_threshold').get_parameter_value().double_value

    def send_obj_clip_req(self, id, atts_to_est, states_to_est):
        self.clip_req = ObjectVisRec.Request()
        self.clip_req.object_id = id
        self.clip_req.class_string = self.semantic_objects[id].class_string
        self.clip_req.attributes_to_estimate = atts_to_est
        self.clip_req.states_to_estimate = states_to_est
        self.clip_req.image = self.semantic_objects[id].image
        self.clip_req.stamp = self.semantic_objects[id].stamp
        resp = self.clip_client.call(self.clip_req)

        for att_dist in resp.attributes:
            att = att_dist.variable
            self.semantic_objects[id].attributes[att].update(att_dist.probabilities, Time.from_msg(resp.stamp))

        for state_dist in resp.states:
            state = state_dist.variable
            self.semantic_objects[id].states[state].update(state_dist.probabilities, Time.from_msg(resp.stamp))

        self.semantic_objects[id].image_available = False

    def compute_match(self, pos):
        similarity_vector = np.zeros(len(self.semantic_objects.keys()))

        for ii,key in enumerate(self.semantic_objects.keys()): 
            similarity_vector[ii] = np.linalg.norm([pos.x - self.semantic_objects[key].pos_x, pos.y - self.semantic_objects[key].pos_y, pos.z - self.semantic_objects[key].pos_z])

        return list(self.semantic_objects.keys())[np.argmin(similarity_vector)]

    def timer_callback(self):
        start_time = self.get_clock().now()

        # For each object, check if state is stale. If so, send state update request.
        for id in self.semantic_objects.keys():

            obj = self.semantic_objects[id]

            if obj.new_image_available==False:
                continue

            states_to_est = []
            atts_to_est = []

            # TODO - modify for a per-variable basis
            for att in obj.attributes:
                if obj.attributes[att].needs_update(start_time):
                    atts_to_est.append(att)
                
            for state in obj.states:
                if obj.states[state].needs_update(start_time):
                    states_to_est.append(state)

            if atts_to_est or tates_to_est:
                self.send_obj_clip_req(id, atts_to_est, states_to_est)

        self.visualize()

        self.get_logger().info("Timer callback time (s): %s" % ((self.get_clock().now() - start_time).nanoseconds/10**9))

    def listener_callback_tracks(self, msg):
        self.tracks_msg = msg

        # Temporary list for this callback
        self.semantic_object_ids = []

        # add new tracked objects from incoming message
        for trkd_obj in msg.tracks:

            if trkd_obj.class_string not in self.objects_of_interest:
                continue

            # Add 
            self.semantic_object_ids.append(trkd_obj.track_id)

            # Initialize object and add to dict if not currently tracked
            if trkd_obj.track_id not in self.semantic_objects.keys():
                self.semantic_objects[trkd_obj.track_id] = SemanticObject(trkd_obj, self.object_params[trkd_obj.class_string])
            else:

                # TODO - make generic semantic_object.update function instead of pos updates
                self.semantic_objects[trkd_obj.track_id].pos_x = trkd_obj.pose.pose.position.x
                self.semantic_objects[trkd_obj.track_id].pos_y = trkd_obj.pose.pose.position.y
                self.semantic_objects[trkd_obj.track_id].pos_z = trkd_obj.pose.pose.position.z

                self.semantic_objects[trkd_obj.track_id].new_image_available = trkd_obj.image_available
                self.semantic_objects[trkd_obj.track_id].image = trkd_obj.image

                self.semantic_objects[trkd_obj.track_id].stamp = msg.header.stamp 

        # Remove untracked objects
        objects_temp = self.semantic_objects
        self.semantic_objects = {}
        self.semantic_objects = {key: val for key, val in objects_temp.items() if key in self.semantic_object_ids}

        self.visualize()


    # def listener_callback_auth(self, msg):
    #     # self.get_logger().info('Received auth message: "%s"' % msg)

    #     # Compute match, perform update
    #     match_key = self.compute_match(msg.pose.pose.position)
    #     self.semantic_objects[match_key].update(msg, 'authentication')

    # def listener_callback_comms(self, msg):
    #     # self.get_logger().info('Received communication message: "%s"' % msg)

    #     comm_matches = {}
    #     for person_id in self.semantic_object_ids:
    #         comm_matches[person_id] = []

    #     # Compute set of comms that are matched to a person
    #     for comm in msg.comms:
    #         match_key = self.compute_match(comm.pose.position)            
    #         comm_matches[match_key].append(comm)

    #     # Update
    #     self.get_logger().info('person-comm matches: "%s"' % comm_matches)
    #     for key in comm_matches.keys():
    #         self.semantic_objects[key].update(comm_matches[key], 'communication')

    # def listener_callback_identity(self, msg):
    #     # self.get_logger().info('Received identity message: "%s"' % msg)

    #     # Compute match, perform update
    #     match_key = self.compute_match(msg.pose.pose.position)
    #     self.semantic_objects[match_key].update(msg, 'identity')


    def visualize(self):
        self.scene_out_msg = SceneUpdate()

        for idx in self.semantic_objects.keys():
            obj = self.semantic_objects[idx]
            entity_msg = SceneEntity()

            # Populate entity message with header / object data
            entity_msg.frame_id = self.tracks_msg.header.frame_id
            entity_msg.timestamp = self.tracks_msg.header.stamp
            entity_msg.id = str(obj.track_id)
            entity_msg.frame_locked = True
            entity_msg.lifetime.nanosec = 500000000

            text = TextPrimitive()
            text.billboard = True
            text.font_size = 16.
            text.scale_invariant = True
            text.color.a = 1.0
            text.pose.position.x = obj.pos_x
            text.pose.position.y = obj.pos_y
            text.pose.position.z = obj.pos_z
            # text.text = "%s #%s: \nID: %s \nAuth: %s\n" % (obj.class_string, obj.track_id, obj.identity, obj.auth)
            text.text = "%s #%s: \n" % (obj.class_string, obj.track_id)
            
            text.text += '\n'
            for att in obj.attributes:
                text.text += "%s: %s %2.0f%%\n" % (att, obj.attributes[att].var_labels[obj.attributes[att].probs.argmax()], 100*obj.attributes[att].probs(obj.attributes[att].probs.argmax()))

            text.text += '\n'
            for state in obj.states:
                text.text += "%s: %s %2.0f%%\n" % (state, obj.states[state].var_labels[obj.states[state].probs.argmax()], 100*obj.states[state].probs(obj.states[state].probs.argmax()))

            # for word in obj.comms.keys():
            #     text.text += "%s (%.2f) " % (word, obj.comms[word]*100)
            entity_msg.texts.append(text)

            self.scene_out_msg.entities.append(entity_msg)

        self.semantic_scene_pub.publish(self.scene_out_msg)


def main(args=None):
    rclpy.init(args=args)

    semantic_tracker_node = SemanticTrackerNode()
    executor = MultiThreadedExecutor()
    executor.add_node(semantic_tracker_node)

    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        semantic_tracker_node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
