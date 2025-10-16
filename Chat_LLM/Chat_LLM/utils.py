# utils.py
"""
Provides common, low-level utility functions for the application.

This module is designed to be free of dependencies on other application modules
(like main_window, ui_widgets, etc.) so it can be safely imported anywhere
without creating circular dependencies.
"""

import sys
import os

def get_asset_path(filename):
    """
    Gets the absolute path to an asset, handling both dev and bundled environments.
    
    Args:
        filename (str): The name of the asset file.
        
    Returns:
        str: The absolute path to the asset file.
    """
    if hasattr(sys, '_MEIPASS'):
        # Running in a PyInstaller bundle
        base_path = os.path.join(sys._MEIPASS, 'assets')
    else:
        # Running in a normal Python environment
        # Get the directory of the current script (e.g., .../project/src)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        # Get the parent directory (the project root, e.g., .../project)
        project_root = os.path.dirname(script_dir)
        # Join with the 'assets' folder
        base_path = os.path.join(project_root, 'assets')
    return os.path.join(base_path, filename)