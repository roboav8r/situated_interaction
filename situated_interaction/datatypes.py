#!/usr/bin/env python3

import gtsam
import numpy as np

from rclpy.time import Time

from situated_interaction.utils import normalize_vector, pmf_to_spec

class Sensor():
    def __init__(self,sym,idx, obs_model_spec):
        self.obs_symbol = gtsam.symbol(sym, idx)
        self.obs_model = gtsam.DiscreteConditional([self.obs_symbol,len(self.obs_labels)],[[self.var_symbol,len(self.var_labels)]],obs_model_spec)

class DiscreteVariable():
    def __init__(self, name, var_type, stamp, labels, sym, idx, init_prob, upper_prob_limit, lower_prob_limit):
        # Admin
        self.name = name
        self.var_type = var_type
        self.last_updated = None
    
        # GTSAM
        self.var_labels = labels
        self.var_symbol = gtsam.symbol(sym, idx)
        
        self.probs = gtsam.DiscreteDistribution((self.var_symbol,len(self.var_labels)), init_prob)

        # Params
        self.upper_prob_limit = upper_prob_limit
        self.lower_prob_limit = lower_prob_limit
        
    def update(self, likelihood, stamp):

        self.probs = gtsam.DiscreteDistribution(likelihood*self.probs)

        normalized_pmf = normalize_vector(self.probs.pmf(), self.upper_prob_limit, self.lower_prob_limit)
        self.probs = gtsam.DiscreteDistribution((self.var_symbol,len(self.var_labels)),normalized_pmf)

        self.last_updated = stamp

class SemanticObject():
    def __init__(self, msg, params):

        # Admin/metadata
        self.track_id = msg.track_id
        self.class_string = msg.class_string
        self.stamp = msg.time_updated

        # Initialize spatial state
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
        
        # Initialize discrete attributes and states
        self.attributes = {}
        for att in params['attributes']:
            self.attributes[att] = DiscreteVariable(att, 'attribute', 
                                                        Time.from_msg(self.stamp), params['attributes'][att]['labels'], params['attributes'][att]['symbol'], 
                                                        self.track_id, params['attributes'][att]['probs'], params['states'][state]['upper_prob_limit'], params['states'][state]['lower_prob_limit'])
        self.states = {}
        for state in params['states']:
            self.states[state] = DiscreteVariable(state, 'state',
                                            Time.from_msg(self.stamp), params['states'][state]['labels'], params['states'][state]['symbol'], 
                                            self.track_id, params['states'][state]['probs'], params['states'][state]['upper_prob_limit'], params['states'][state]['lower_prob_limit'])

        # Initialize communication
        self.comms = DiscreteVariable('comms', 'comms',
                                            Time.from_msg(self.stamp), params['comms']['labels'], params['comms']['symbol'], 
                                            self.track_id, params['comms']['probs'], params['comms']['upper_prob_limit'], params['comms']['lower_prob_limit'])

    def update_spatial_state(self, tracked_object_msg):
                
        self.pos_x = tracked_object_msg.pose.pose.position.x
        self.pos_y = tracked_object_msg.pose.pose.position.y
        self.pos_z = tracked_object_msg.pose.pose.position.z

        self.new_image_available = tracked_object_msg.image_available
        self.image = tracked_object_msg.image

        self.stamp = tracked_object_msg.time_updated

    def update_comms(self, likelihood, stamp):
        self.comms.update(likelihood, stamp)

    def update_verbal_comms(self, transcript, confidence, parent_node):
        
        # parent_node.get_logger().info("Comms labels %s" % (self.comm_labels))
        # parent_node.get_logger().info("update comms with transcript %s confidence %s" % (transcript, confidence))
        
        try:
            command_idx = self.comm_transcripts.index(transcript)
        except ValueError:
            if transcript=='':
                command_idx = 0 # Handle null commands
            else:
                command_idx = 1 # Handle "other" commands that are not in list
        # parent_node.get_logger().info("Command index %s" % (command_idx))

        likelihood = self.comm_verbal_obs_model.likelihood(command_idx)
        # parent_node.get_logger().info("Likelihood %s" % (likelihood))
        self.comm_probs = gtsam.DiscreteDistribution(likelihood*self.comm_probs)
        # parent_node.get_logger().info("New comm probs %s" % (self.comm_probs))

        normalized_pmf = normalize_vector(self.comm_probs.pmf(), self.upper_prob_limit, self.lower_prob_limit)
        self.comm_probs = gtsam.DiscreteDistribution((self.comm_var_symbol,len(self.comm_labels)),normalized_pmf)
        # parent_node.get_logger().info("Normalized comm probs %s" % (self.comm_probs))

        # TODO
        # TODO - handle null command/empty command
        # TODO - handle "other" command not in list


    def update_gesture_comms(self, gest_dist, parent_node):
        
        command_idx = np.argmax(gest_dist.probabilities)

        likelihood = self.comm_gesture_obs_model.likelihood(command_idx)
        self.comm_probs = gtsam.DiscreteDistribution(likelihood*self.comm_probs)

        normalized_pmf = normalize_vector(self.comm_probs.pmf(), self.upper_prob_limit, self.lower_prob_limit)
        self.comm_probs = gtsam.DiscreteDistribution((self.comm_var_symbol,len(self.comm_labels)),normalized_pmf)
