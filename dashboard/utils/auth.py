"""
Authentication utilities for dashboard
"""
import bcrypt
import yaml
import os
from pathlib import Path

USERS_FILE = Path(__file__).parent.parent.parent / "config/dashboard_users.yaml"


def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against a hash"""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))


def load_users():
    """Load users from YAML file"""
    if not USERS_FILE.exists():
        # Create default user if file doesn't exist
        default_password = hash_password("mwgitano40")
        users_data = {
            "users": [
                {
                    "username": "telge",
                    "password_hash": default_password,
                    "role": "admin"
                }
            ]
        }
        with open(USERS_FILE, 'w') as f:
            yaml.dump(users_data, f)
        return users_data
    
    with open(USERS_FILE, 'r') as f:
        return yaml.safe_load(f)


def authenticate_user(username: str, password: str) -> dict | None:
    """Authenticate a user"""
    users_data = load_users()
    
    for user in users_data.get("users", []):
        if user["username"] == username:
            if verify_password(password, user["password_hash"]):
                return {
                    "username": user["username"],
                    "role": user.get("role", "viewer")
                }
            break
    
    return None


def get_user_role(username: str) -> str:
    """Get user role"""
    users_data = load_users()
    
    for user in users_data.get("users", []):
        if user["username"] == username:
            return user.get("role", "viewer")
    
    return "viewer"

