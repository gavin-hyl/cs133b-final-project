import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Vector3
from sensor_msgs.msg import PointCloud2
from sensor_msgs_py import point_cloud2
from multi_slam.Map import MAP
from std_msgs.msg import Header
import numpy as np
from visualization_msgs.msg import Marker
import math
from shapely.geometry import LineString, Point, Polygon


class PhysicsSimNode(Node):
    def __init__(self):
        super().__init__("physics_sim")
        self.control_signal_sub = self.create_subscription(
            Vector3, "control_signal", self.control_signal_cb, 10
        )

        self.declare_parameter("lidar_r_max", 5)
        self.lidar_r_max = self.get_parameter("lidar_r_max").value

        self.declare_parameter("lidar_r_min", 0.2)
        self.lidar_r_min = self.get_parameter("lidar_r_min").value

        self.declare_parameter("lidar_delta_theta", 3)
        self.lidar_delta_theta = self.get_parameter("lidar_delta_theta").value

        self.declare_parameter("lidar_std_dev", 0)
        self.lidar_std_dev = self.get_parameter("lidar_std_dev").value

        self.declare_parameter("beacon_std_dev", 0)
        self.beacon_std_dev = self.get_parameter("beacon_std_dev").value

        self.declare_parameter("pos_std_dev_dist", 0.1)
        self.pos_std_dev_dist = self.get_parameter("pos_std_dev_dist").value

        self.declare_parameter("pos_std_dev_theta", 0.05)
        self.pos_std_dev_theta = self.get_parameter("pos_std_dev_theta").value

        # Change parameter name to reflect that noise is applied to velocity
        self.declare_parameter("vel_std_dev", 0.1)  # New parameter for velocity noise
        self.vel_std_dev = self.get_parameter("vel_std_dev").value

        # Robot dimensions parameters
        self.declare_parameter("robot_length", 0.4)
        self.robot_length = self.get_parameter("robot_length").value
        
        self.declare_parameter("robot_width", 0.3)
        self.robot_width = self.get_parameter("robot_width").value
        
        # Increase buffer for collision detection
        self.declare_parameter("collision_buffer", 0.1)  # Buffer distance from obstacles
        self.collision_buffer = self.get_parameter("collision_buffer").value
        
        # Make collision increment smaller for more precise detection
        self.declare_parameter("collision_increment", 0.02)  # Smaller increment
        self.collision_increment = self.get_parameter("collision_increment").value

        self.declare_parameter("sim_dt", 0.1)
        self.sim_dt = self.get_parameter("sim_dt").value

        self.lidar_pub = self.create_publisher(PointCloud2, "lidar", 10)
        self.beacon_pub = self.create_publisher(PointCloud2, "beacon", 10)

        self.pos_ideal_viz_pub = self.create_publisher(Marker, "visualization_marker_ideal", 10)
        self.pos_true_viz_pub = self.create_publisher(Marker, "visualization_marker_true", 10)
        self.beacon_viz_pub = self.create_publisher(PointCloud2, "beacon_viz", 10)
        self.lidar_viz_pub = self.create_publisher(PointCloud2, "lidar_viz", 10)

        self.lidar_pub_timer = self.create_timer(0.1, self.lidar_publish_cb)
        self.beacon_pub_timer = self.create_timer(0.1, self.beacon_publish_cb)
        self.sim_update_timer = self.create_timer(self.sim_dt, self.sim_update_cb)

        self.pos_ideal = np.array([2, 2, 0], dtype=float)  # Ideal position without noise
        self.pos_true = np.array([2, 2, 0], dtype=float)   # True physical position with velocity noise
        self.vel_true = np.array([0, 0, 0], dtype=float)   # Velocity with noise (slippage)
        self.vel_ideal = np.array([0, 0, 0], dtype=float)  # Commanded velocity
        self.pos_est = np.array([0, 0, 0], dtype=float)
        self.accel = np.array([0, 0, 0], dtype=float)
        self.control_signal_received = False  # Flag to track if control signal was received

    def control_signal_cb(self, msg: Vector3):
        # Store the commanded velocity as ideal velocity
        self.vel_ideal = np.array([msg.x, msg.y, 0.0])
        
        # Create noisy velocity to simulate slippage
        noise = np.random.normal(0, self.vel_std_dev, 3)
        noise[2] = 0.0  # Keep z component at 0
        self.vel_true = self.vel_ideal + noise
        
        # Rather than setting a flag, update position immediately to reduce lag
        # Calculate new positions based on velocities
        intended_ideal_pos = self.pos_ideal + self.vel_ideal * self.sim_dt
        intended_true_pos = self.pos_true + self.vel_true * self.sim_dt
        
        # Update ideal position immediately
        self.pos_ideal = intended_ideal_pos
        
        # Check collision for true position and update it
        self.pos_true = self.check_collision(self.pos_true, intended_true_pos)
        
        # Still set the flag for the timer-based updates
        self.control_signal_received = True

    def _apply_2d_noise(self, points: np.array, std_dev: float):
        noisy_points = []
        for point in points:
            noisy_point = np.array(
                [
                    point[0] + np.random.normal(0, std_dev),
                    point[1] + np.random.normal(0, std_dev),
                    0,
                ]
            )
            noisy_points.append(noisy_point)
        return noisy_points

    def _apply_3d_noise(self, points: np.array, std_dev_dist: float, std_dev_theta: float):
        noisy_points = []
        for point in points:
            noisy_point = np.array(
                [
                    point[0] + np.random.normal(0, std_dev_dist),
                    point[1] + np.random.normal(0, std_dev_dist),
                    point[2] + np.random.normal(0, std_dev_theta),
                ]
            )
            noisy_points.append(noisy_point)
        return noisy_points

    def create_robot_polygon(self, position):
        """
        Creates a circular buffer around the robot position point
        """
        # Extract position
        x, y, _ = position
        
        # Create a circular buffer around the point
        # Use the collision_buffer parameter as the radius
        point = Point(x, y)
        circle = point.buffer(self.collision_buffer)
        
        return circle

    def check_collision(self, current_pos, intended_pos):
        """
        Enhanced collision check using a point-based model with buffer
        """
        # Create a circular buffer at the intended position
        robot_circle = self.create_robot_polygon(intended_pos)
        
        # Check if the circle intersects with any obstacles or the boundary
        obstacles_intersect = False
        for obstacle in MAP.obstacles:
            if robot_circle.intersects(obstacle):
                obstacles_intersect = True
                break
                
        # Check boundary intersection
        boundary_intersect = robot_circle.intersects(MAP.boundary)
        
        # If no collision, return the intended position
        if not obstacles_intersect and not boundary_intersect:
            return intended_pos
            
        # Calculate direction from current to intended position
        direction = np.array([
            intended_pos[0] - current_pos[0],
            intended_pos[1] - current_pos[1],
            intended_pos[2] - current_pos[2]
        ])
        
        # Calculate distance between current and intended position
        distance = math.sqrt(direction[0]**2 + direction[1]**2)
        
        # If no movement, return current position
        if distance < 1e-6:
            return current_pos
            
        # Normalize direction vector (for x, y components)
        if distance > 0:
            direction[0] /= distance
            direction[1] /= distance
        
        # Start from current position and move incrementally
        safe_pos = np.array(current_pos)  # Last known safe position
        test_pos = np.array(current_pos)
        increment_distance = 0.0
        
        # Use smaller increments to ensure we don't miss collisions
        while increment_distance < distance:
            # Move forward by increment
            increment_distance += self.collision_increment
            if increment_distance >= distance:
                # We've reached the full distance
                break
                
            # Calculate the new test position
            test_pos = np.array([
                current_pos[0] + direction[0] * increment_distance,
                current_pos[1] + direction[1] * increment_distance,
                current_pos[2] + (intended_pos[2] - current_pos[2]) * (increment_distance / distance)
            ])
            
            # Create robot polygon at test position
            test_poly = self.create_robot_polygon(test_pos)
            
            # Check for collisions at test position
            test_collides = False
            for obstacle in MAP.obstacles:
                if test_poly.intersects(obstacle):
                    test_collides = True
                    break
                    
            if test_poly.intersects(MAP.boundary):
                test_collides = True
                
            # If collision detected, return the last safe position
            if test_collides:
                return safe_pos
            
            # Update the safe position
            safe_pos = np.array(test_pos)
                
        # If we completed the loop without collision, return the intended position
        # This handles the case where the last increment might go past the intended position
        if not test_collides:
            return intended_pos
        else:
            return safe_pos

    def lidar_publish_cb(self):
        # Use the true position (with noise) for LiDAR measurements
        points = MAP.calc_lidar_point_cloud(
            self.pos_true,
            self.lidar_delta_theta,
            self.lidar_r_max,
            self.lidar_r_min,
        )
        # Apply additional sensor noise to the points
        noisy_points = self._apply_2d_noise(points, self.lidar_std_dev)
        
        header = Header()
        header.stamp = self.get_clock().now().to_msg()
        header.frame_id = "map"
        lidar_msg = point_cloud2.create_cloud_xyz32(header, noisy_points)
        self.lidar_pub.publish(lidar_msg)

        lidar_points_world = []
        for point in noisy_points:
            lidar_points_world.append(
                np.array(
                    [
                        point[0] + self.pos_true[0],
                        point[1] + self.pos_true[1],
                        0,
                    ]
                )
            )
        lidar_points_world_msg = point_cloud2.create_cloud_xyz32(
            header, lidar_points_world
        )
        self.lidar_viz_pub.publish(lidar_points_world_msg)

    def beacon_publish_cb(self):
        # Use the true position (with noise) for beacon measurements
        points = MAP.calc_beacon_positions(self.pos_true)
        # Apply sensor noise
        noisy_points = self._apply_2d_noise(points, self.beacon_std_dev)
        
        header = Header()
        header.stamp = self.get_clock().now().to_msg()
        header.frame_id = "map"
        beacon_msg = point_cloud2.create_cloud_xyz32(header, noisy_points)
        self.beacon_pub.publish(beacon_msg)

        beacon_positions_world = []
        for point in noisy_points:
            beacon_positions_world.append(
                np.array(
                    [
                        point[0] + self.pos_true[0],
                        point[1] + self.pos_true[1],
                        0,
                    ]
                )
            )
        beacon_positions_world_msg = point_cloud2.create_cloud_xyz32(
            header, beacon_positions_world
        )
        self.beacon_viz_pub.publish(beacon_positions_world_msg)

    def publish_ideal_pos(self):
        """Publish the ideal position marker (theoretical position without noise)"""
        marker = Marker()
        marker.header.stamp = self.get_clock().now().to_msg()
        marker.header.frame_id = "map"
        marker.ns = "ideal_position"
        marker.id = 0
        marker.type = Marker.SPHERE
        marker.action = Marker.ADD

        # Set the position to the ideal position
        marker.pose.position.x = self.pos_ideal[0]
        marker.pose.position.y = self.pos_ideal[1]
        marker.pose.position.z = self.pos_ideal[2]

        # No rotation needed
        marker.pose.orientation.x = 0.0
        marker.pose.orientation.y = 0.0
        marker.pose.orientation.z = 0.0
        marker.pose.orientation.w = 1.0

        # For a sphere with radius 0.3
        marker.scale.x = 0.6
        marker.scale.y = 0.6
        marker.scale.z = 0.6

        # Blue color for ideal position
        marker.color.r = 0.0
        marker.color.g = 0.0
        marker.color.b = 1.0
        marker.color.a = 1.0  # Fully opaque

        self.pos_ideal_viz_pub.publish(marker)

    def publish_true_pos(self):
        """Publish the true position marker (actual physical position with noise)"""
        marker = Marker()
        marker.header.stamp = self.get_clock().now().to_msg()
        marker.header.frame_id = "map"
        marker.ns = "true_position"
        marker.id = 1
        marker.type = Marker.SPHERE
        marker.action = Marker.ADD

        # Set the position to the true position
        marker.pose.position.x = self.pos_true[0]
        marker.pose.position.y = self.pos_true[1]
        marker.pose.position.z = self.pos_true[2]

        # No rotation needed
        marker.pose.orientation.x = 0.0
        marker.pose.orientation.y = 0.0
        marker.pose.orientation.z = 0.0
        marker.pose.orientation.w = 1.0

        # For a sphere with radius 0.3
        marker.scale.x = 0.6
        marker.scale.y = 0.6
        marker.scale.z = 0.6

        # Green color for true position
        marker.color.r = 0.0
        marker.color.g = 1.0
        marker.color.b = 0.0
        marker.color.a = 1.0  # Fully opaque

        self.pos_true_viz_pub.publish(marker)

    def sim_update_cb(self):
        # Check if there's any movement (non-zero velocity)
        is_moving = not np.allclose(self.vel_ideal, [0, 0, 0], atol=1e-6)
        
        if is_moving and self.control_signal_received:
            # Most position updates now happen in control_signal_cb for responsiveness
            # This is a backup update for continuous movement
            
            # Update positions if we're still moving but haven't received new controls
            intended_ideal_pos = self.pos_ideal + self.vel_ideal * self.sim_dt
            intended_true_pos = self.pos_true + self.vel_true * self.sim_dt
            
            self.pos_ideal = intended_ideal_pos
            self.pos_true = self.check_collision(self.pos_true, intended_true_pos)
        
        # Publish markers and sensor data
        self.publish_ideal_pos()
        self.publish_true_pos()


def main(args=None):
    rclpy.init(args=args)
    node = PhysicsSimNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        print("Exiting...")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()