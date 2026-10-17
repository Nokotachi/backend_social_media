class UsersError(Exception):
    pass


class ProfileNotFound(UsersError):
    pass


class CannotFollowYourself(UsersError):
    pass


class WrongPassword(UsersError):
    pass


class CannotBlockAdmin(UsersError):
    pass
