# -*- coding: utf-8 -*-
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

# Full list of supported languages for the translation layer
# Non-ASCII characters are escaped to prevent encoding errors on different systems.
LANGUAGES = {
    "aa": "Afar", "af": "Afrikaans", "ak": "Akan", "am": "Amharic", "ar": "Arabic", "as": "Assamese",
    "az": "Azerbaijani", "ba": "Bashkir", "be": "Belarusian", "bg": "Bulgarian", "bm": "Bambara",
    "bn": "Bengali", "bo": "Tibetan", "br": "Breton", "bs": "Bosnian", "ca": "Catalan", "ce": "Chechen",
    "co": "Corsican", "cs": "Czech", "cv": "Chuvash", "cy": "Welsh", "da": "Danish", "de": "German",
    "dv": "Divehi", "dz": "Dzongkha", "ee": "Ewe", "el": "Greek", "en": "English", "eo": "Esperanto",
    "es": "Spanish", "et": "Estonian", "eu": "Basque", "fa": "Persian", "ff": "Fulah", "fi": "Finnish",
    "fil": "Filipino", "fo": "Faroese", "fr": "French", "fy": "Western Frisian", "ga": "Irish",
    "gd": "Scottish Gaelic", "gl": "Galician", "gn": "Guarani", "gu": "Gujarati", "gv": "Manx",
    "ha": "Hausa", "he": "Hebrew", "hi": "Hindi", "hr": "Croatian", "ht": "Haitian", "hu": "Hungarian",
    "hy": "Armenian", "ia": "Interlingua", "id": "Indonesian", "ie": "Interlingue", "ig": "Igbo",
    "ii": "Sichuan Yi", "ik": "Inupiaq", "io": "Ido", "is": "Icelandic", "it": "Italian", "iu": "Inuktitut",
    "ja": "Japanese", "jv": "Javanese", "ka": "Georgian", "ki": "Kikuyu", "kk": "Kazakh", "kl": "Kalaallisut",
    "km": "Khmer", "kn": "Kannada", "ko": "Korean", "ks": "Kashmiri", "ku": "Kurdish", "kw": "Cornish",
    "ky": "Kyrgyz", "la": "Latin", "lb": "Luxembourgish", "lg": "Ganda", "ln": "Lingala", "lo": "Lao",
    "lt": "Lithuanian", "lu": "Luba-Katanga", "lv": "Latvian", "mg": "Malagasy", "mi": "Maori",
    "mk": "Macedonian", "ml": "Malayalam", "mn": "Mongolian", "mr": "Marathi", "ms": "Malay",
    "mt": "Maltese", "my": "Burmese", "nb": "Norwegian Bokm\u00e5l", "nd": "North Ndebele", "ne": "Nepali",
    "nl": "Dutch", "nn": "Norwegian Nynorsk", "no": "Norwegian", "nr": "South Ndebele", "nv": "Navajo",
    "ny": "Chichewa", "oc": "Occitan", "om": "Oromo", "or": "Oriya", "os": "Ossetian", "pa": "Punjabi",
    "pl": "Polish", "ps": "Pashto", "pt": "Portuguese", "qu": "Quechua", "rm": "Romansh", "rn": "Rundi",
    "ro": "Romanian", "ru": "Russian", "rw": "Kinyarwanda", "sa": "Sanskrit", "sc": "Sardinian",
    "sd": "Sindhi", "se": "Northern Sami", "sg": "Sango", "si": "Sinhala", "sk": "Slovak", "sl": "Slovenian",
    "sn": "Shona", "so": "Somali", "sq": "Albanian", "sr": "Serbian", "ss": "Swati", "st": "Southern Sotho",
    "su": "Sundanese", "sv": "Swedish", "sw": "Swahili", "ta": "Tamil", "te": "Telugu", "tg": "Tajik",
    "th": "Thai", "ti": "Tigrinya", "tk": "Turkmen", "tl": "Tagalog", "tn": "Tswana", "to": "Tonga",
    "tr": "Turkish", "ts": "Tsonga", "tt": "Tatar", "ug": "Uyghur", "uk": "Ukrainian", "ur": "Urdu",
    "uz": "Uzbek", "ve": "Venda", "vi": "Vietnamese", "vo": "Volap\u00fck", "wa": "Walloon", "wo": "Wolof",
    "xh": "Xhosa", "yi": "Yiddish", "yo": "Yoruba", "za": "Zhuang", "zh": "Chinese", "zu": "Zulu"
}